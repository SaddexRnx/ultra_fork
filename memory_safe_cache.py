"""
memory_safe_cache.py
--------------------
Persistent, low-memory caching and domain-level rate limiting for HTTP
responses.

Storage back-end: sqlite3 (stdlib) – no extra dependencies, zero memory
overhead when idle.

Two components:
  1. ``ResponseCache`` – stores raw HTML and response metadata keyed by
     URL SHA-256 hash.  Old entries are pruned by a TTL.
  2. ``TokenBucket`` – per-domain token-bucket rate limiter that sleeps
     when the bucket is empty.
"""

import json
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from ultra_stealth_fetcher import url_hash

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = os.path.join(tempfile.gettempdir(), "scraping_cache.db")
_DEFAULT_TTL_SECONDS = 3600  # 1 hour
_DEFAULT_BUCKET_CAPACITY = 10
_DEFAULT_REFILL_RATE = 2.0  # tokens per second


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------

@dataclass
class CachedResponse:
    """Deserialised representation of a cached HTTP response."""
    status: int
    headers: Dict[str, str]
    body: str
    cached_at: float


class ResponseCache:
    """
    SQLite-backed persistent cache for HTML responses.

    Thread-safe via a single ``threading.Lock``.  The table is created on
    first instantiation.

    Usage::

        cache = ResponseCache("my_cache.db")
        await cache.set("https://...", 200, {"content-type": "..."}, "<html>...")
        entry = await cache.get("https://...")
    """

    def __init__(self, db_path: str = _DEFAULT_DB_PATH, ttl: int = _DEFAULT_TTL_SECONDS) -> None:
        self._db_path = db_path
        self._ttl = ttl
        self._lock = threading.Lock()

        with self._lock:
            self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return a new connection (thread-safe pattern)."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode = WAL")       # better concurrency
        conn.execute("PRAGMA synchronous = NORMAL")      # balance speed/durability
        return conn

    def _init_db(self) -> None:
        """Create the cache table if it does not exist."""
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS response_cache (
                    url_hash TEXT PRIMARY KEY,
                    status   INTEGER NOT NULL,
                    headers  TEXT NOT NULL,
                    body     TEXT NOT NULL,
                    cached_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cached_at ON response_cache(cached_at)"
            )

    # ------------------------------------------------------------------
    # Public API  (async-friendly — I/O is disk I/O so we keep sync
    # internally; callers that are async should just await these, or
    # we expose a thread-pool wrapper.  For simplicity we offer both.)
    # ------------------------------------------------------------------

    async def get(self, url: str) -> Optional[CachedResponse]:
        """
        Return a ``CachedResponse`` for ``url``, or ``None`` if not present
        / expired.
        """
        key = url_hash(url)
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT status, headers, body, cached_at FROM response_cache WHERE url_hash = ?",
                    (key,),
                ).fetchone()

        if row is None:
            return None

        status, headers_json, body, cached_at = row
        age = time.time() - cached_at
        if age > self._ttl:
            await self.delete(url)
            return None

        return CachedResponse(
            status=status,
            headers=json.loads(headers_json),
            body=body,
            cached_at=cached_at,
        )

    async def set(
        self,
        url: str,
        status: int,
        headers: Dict[str, str],
        body: str,
    ) -> None:
        """Insert or replace a cache entry."""
        key = url_hash(url)
        now = time.time()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO response_cache
                        (url_hash, status, headers, body, cached_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, status, json.dumps(headers), body, now),
                )

    async def delete(self, url: str) -> None:
        """Remove a single entry from the cache."""
        key = url_hash(url)
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "DELETE FROM response_cache WHERE url_hash = ?", (key,)
                )

    async def clear_expired(self) -> int:
        """Remove all entries older than TTL; return count of deleted rows."""
        cutoff = time.time() - self._ttl
        with self._lock:
            with self._conn() as conn:
                result = conn.execute(
                    "DELETE FROM response_cache WHERE cached_at < ?", (cutoff,)
                )
                return result.rowcount

    async def clear_all(self) -> None:
        """Drop every row in the cache."""
        with self._lock:
            with self._conn() as conn:
                conn.execute("DELETE FROM response_cache")


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (per-domain)
# ---------------------------------------------------------------------------

@dataclass
class _BucketState:
    tokens: float
    last_refill: float = field(default_factory=time.time)


class DomainRateLimiter:
    """
    Simple per-domain token-bucket rate limiter.

    Each domain gets its own bucket.  Before issuing a request, call
    ``acquire(domain)`` which will block (sleep) if the bucket is empty.

    Usage::

        limiter = DomainRateLimiter(capacity=10, refill_rate=2.0)
        await limiter.acquire("example.com")
        # ... make request ...
    """

    def __init__(
        self,
        capacity: float = _DEFAULT_BUCKET_CAPACITY,
        refill_rate: float = _DEFAULT_REFILL_RATE,
    ) -> None:
        """
        :param capacity:   Maximum number of tokens a bucket can hold.
        :param refill_rate: Tokens added per second (fractional OK).
        """
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._buckets: Dict[str, _BucketState] = {}
        self._lock = threading.Lock()

    def _get_bucket(self, domain: str) -> _BucketState:
        if domain not in self._buckets:
            self._buckets[domain] = _BucketState(tokens=self._capacity)
        return self._buckets[domain]

    def _refill(self, bucket: _BucketState) -> None:
        now = time.time()
        elapsed = now - bucket.last_refill
        bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_rate)
        bucket.last_refill = now

    def acquire(self, domain: str) -> float:
        """
        Block until a token is available for *domain*.

        Returns the wait time (0.0 if the request should proceed immediately).
        The caller is responsible for sleeping if > 0.
        """
        with self._lock:
            bucket = self._get_bucket(domain)
            self._refill(bucket)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return 0.0
            # How long until the next token?
            deficit = 1.0 - bucket.tokens
            wait = deficit / self._refill_rate
            # "Borrow" the token so other concurrent calls see the correct
            # state; the wait compensates.
            bucket.tokens = 0.0
            return wait

    def acquire_async(self, domain: str) -> float:
        """Non-blocking: returns wait seconds (caller must ``await asyncio.sleep(wait)``)."""
        return self.acquire(domain)
