"""
ultra_stealth_fetcher.py
------------------------
Thin async wrapper around Scrapling's battle-tested ``AsyncFetcher``.

Eliminates manual ``curl_cffi`` management — delegates TLS fingerprinting,
connection pooling, proxy rotation, and stealth headers to the Scrapling
engine that is already tested on thousands of targets.

The public API matches the original contract so ``main.py`` and
``memory_safe_cache.py`` require no changes.
"""

import asyncio
import random
from hashlib import sha256

from scrapling.fetchers import AsyncFetcher

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

_RETRYABLE_CODES = {403, 429, 503}
_DEFAULT_RETRIES = 3
_BASE_BACKOFF = 1.0
_MAX_BACKOFF = 30.0


# ---------------------------------------------------------------------------
# URL hashing  (kept here because memory_safe_cache.py imports this)
# ---------------------------------------------------------------------------

def url_hash(url: str) -> str:
    """Deterministic SHA-256 hex digest of a URL."""
    return sha256(url.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------

def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at ``_MAX_BACKOFF``."""
    delay = min(_BASE_BACKOFF * (2 ** (attempt - 1)), _MAX_BACKOFF)
    jitter = random.uniform(0, 0.5 * delay)
    return delay + jitter


# ---------------------------------------------------------------------------
# Core fetch wrapper
# ---------------------------------------------------------------------------

async def fetch(
    url: str,
    *,
    impersonate: str = "chrome120",
    timeout: float = 30.0,
    follow_redirects: bool = True,
    max_redirects: int = 10,
) -> dict:
    """
    Fetch *url* via Scrapling's ``AsyncFetcher`` with Chrome 120
    impersonation and stealth headers.

    Returns the same dict shape as the original implementation::

        {"status": int, "headers": dict, "body": str, "url": str, "cached": bool}

    Raises ``RuntimeError`` when all retries are exhausted.
    """
    for attempt in range(1, _DEFAULT_RETRIES + 1):
        try:
            response = await AsyncFetcher.get(
                url,
                impersonate=impersonate,
                stealthy_headers=True,
                timeout=timeout,
                follow_redirects=follow_redirects,
                max_redirects=max_redirects,
            )

            status = response.status

            # Retry on 403 / 429 / 503
            if status in _RETRYABLE_CODES:
                if attempt < _DEFAULT_RETRIES:
                    print(
                        f"Retry {attempt} failed for {url}: "
                        f"HTTP {status} – backing off"
                    )
                    await asyncio.sleep(_backoff_delay(attempt))
                    continue
                raise RuntimeError(
                    f"Max retries ({_DEFAULT_RETRIES}) exhausted for "
                    f"{url} (last status={status})"
                )

            # Decode the raw body
            body_bytes = response.body
            body_str = body_bytes.decode("utf-8", errors="replace")

            return {
                "status": status,
                "headers": dict(response.headers),
                "body": body_str,
                "url": str(response.url),
                "cached": False,
            }

        except Exception as exc:
            if attempt < _DEFAULT_RETRIES:
                print(f"Retry {attempt} failed for {url}: {exc}")
                await asyncio.sleep(_backoff_delay(attempt))
                continue
            raise RuntimeError(
                f"Fetch failed for {url} after {_DEFAULT_RETRIES} retries: {exc}"
            ) from exc

    raise RuntimeError(f"Unreachable: {url}")  # pragma: no cover
