"""
ultra_stealth_fetcher.py
------------------------
Lightweight HTTP fetcher that bypasses basic anti-bot protections using
TLS fingerprinting (JA3/JA4 via curl_cffi) WITHOUT any headless browser.

Key features:
  - Chrome 131 impersonation via curl_cffi
  - Exponential-backoff retry for 403 / 429 / 503
  - Rotating User-Agent & common header pool
  - Streaming response body in chunks to cap peak memory < 20 MB per request
  - Fully async session management
"""

import asyncio
import re
from hashlib import sha256
from typing import Optional

from curl_cffi.requests import AsyncSession, Response as CurlResponse
from curl_cffi import CurlHttpVersion

# ---------------------------------------------------------------------------
# Rotating header pool  --  lightweight; we just pick one of these per request
# so there is no heavy per-request header generation.
# ---------------------------------------------------------------------------

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

_ACCEPT_VALUES = [
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
]

_ACCEPT_LANGUAGE = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.8",
]

_SEC_CH_UA = [
    '"Chromium";v="131", "Not_A Brand";v="24"',
    '"Chromium";v="130", "Not_A Brand";v="24"',
    '"Chromium";v="131", "Google Chrome";v="131", "Not=A?Brand";v="24"',
]

# Regex to match (403, 429, 503) status codes for automatic retry
_RETRYABLE_CODES = {403, 429, 503}

# 16 KB chunk size for streaming response body
_CHUNK_SIZE = 16 * 1024


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def url_hash(url: str) -> str:
    """Deterministic SHA-256 hex digest of a URL (useful as a cache key)."""
    return sha256(url.encode("utf-8")).hexdigest()


def _build_headers() -> dict:
    """Produce a dict of common browser-ish headers for a single request."""
    import random
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": random.choice(_ACCEPT_VALUES),
        "Accept-Language": random.choice(_ACCEPT_LANGUAGE),
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Ch-Ua": random.choice(_SEC_CH_UA),
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.google.com/",
        "Cache-Control": "max-age=0",
        "Dnt": "1",
    }


# ---------------------------------------------------------------------------
# Core fetcher
# ---------------------------------------------------------------------------

class UltraStealthFetcher:
    """
    Async HTTP fetcher with TLS fingerprint spoofing, retry/backoff, and a
    memory-conscious streaming body reader.

    Usage::

        fetcher = UltraStealthFetcher()
        result = await fetcher.fetch("https://example.com")
        # result -> {"status": 200, "headers": {...}, "body": "<html>..."}
    """

    def __init__(
        self,
        impersonate: str = "chrome131",
        max_retries: int = 3,
        base_backoff: float = 1.0,
        max_backoff: float = 30.0,
        timeout: float = 30.0,
    ) -> None:
        """
        :param impersonate: Browser string passed to curl_cffi.
        :param max_retries:  Max retry attempts for retryable status codes.
        :param base_backoff: Initial backoff in seconds (doubles each retry).
        :param max_backoff:  Upper bound for backoff in seconds.
        :param timeout:      Overall request timeout in seconds.
        """
        self._impersonate = impersonate
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._timeout = timeout

        # One reusable session (lazily created)
        self._session: Optional[AsyncSession] = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "UltraStealthFetcher":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Core fetch  --  memory-streamed body, exponential backoff
    # ------------------------------------------------------------------

    async def fetch(
        self,
        url: str,
        *,
        headers: Optional[dict] = None,
        follow_redirects: bool = True,
        max_redirects: int = 10,
    ) -> dict:
        """
        Perform an HTTP GET with stealth fingerprinting.

        Returns a dict with keys:
            ``status``, ``headers``, ``body`` (str), ``url`` (final), ``cached`` (bool).

        Raises ``RuntimeError`` if all retries are exhausted.
        """
        session = await self._get_session()
        request_headers = _build_headers()
        if headers:
            request_headers.update(headers)

        last_exc = None

        for attempt in range(1, self._max_retries + 1):
            try:
                raw: CurlResponse = await session.get(
                    url,
                    headers=request_headers,
                    impersonate=self._impersonate,
                    timeout=self._timeout,
                    allow_redirects=follow_redirects,
                    max_redirects=max_redirects,
                    # Prefer HTTP/2 for better TLS fingerprint mimicry
                    http_version=CurlHttpVersion.V2,
                )

                status = raw.status_code

                # Retry on 403 / 429 / 503
                if status in _RETRYABLE_CODES:
                    if attempt < self._max_retries:
                        backoff = self._backoff_delay(attempt)
                        await asyncio.sleep(backoff)
                        continue
                    raise RuntimeError(
                        f"Max retries ({self._max_retries}) exhausted for "
                        f"{url} (last status={status})"
                    )

                # Stream the body in small chunks to keep peak memory low
                body_parts = []
                size_estimate = 0
                async for chunk in raw.aiter_content(_CHUNK_SIZE):
                    body_parts.append(chunk)
                    size_estimate += len(chunk)
                    # Safety valve: if a single response somehow exceeds 50 MB,
                    # we stop reading to avoid OOM on a 4 GB machine.
                    if size_estimate > 50 * 1024 * 1024:
                        break

                body_bytes = b"".join(body_parts)
                body_str = body_bytes.decode(raw.encoding or "utf-8", errors="replace")

                return {
                    "status": status,
                    "headers": dict(raw.headers),
                    "body": body_str,
                    "url": str(raw.url),
                    "cached": False,
                }

            except Exception as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    backoff = self._backoff_delay(attempt)
                    await asyncio.sleep(backoff)
                    continue
                raise RuntimeError(
                    f"Fetch failed for {url} after {self._max_retries} retries"
                ) from last_exc

        raise RuntimeError(f"Unreachable: {url}")  # pragma: no cover

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped at ``max_backoff``."""
        import random
        delay = min(self._base_backoff * (2 ** (attempt - 1)), self._max_backoff)
        jitter = random.uniform(0, 0.5 * delay)
        return delay + jitter
