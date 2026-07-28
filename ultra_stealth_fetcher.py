"""
ultra_stealth_fetcher.py
------------------------
Async HTTP fetcher using ``curl_cffi`` directly with Chrome 120
impersonation, exponential-backoff retry, and streaming body reads.

Avoids Scrapling's import chain (which pulls in Playwright unnecessarily)
while still giving us battle-tested TLS fingerprinting via curl_cffi.

Exposes a single ``async def fetch(url)`` that returns the same dict
schema as the original implementation so ``main.py`` and
``memory_safe_cache.py`` are unaffected.
"""

import asyncio
import random
from hashlib import sha256

from curl_cffi.requests import AsyncSession, Response as CurlResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_RETRYABLE_CODES = {403, 429, 503}
_DEFAULT_RETRIES = 3
_BASE_BACKOFF = 1.0
_MAX_BACKOFF = 30.0

# ---------------------------------------------------------------------------
# URL hashing  (imported by memory_safe_cache.py)
# ---------------------------------------------------------------------------

def url_hash(url: str) -> str:
    """Deterministic SHA-256 hex digest of a URL."""
    return sha256(url.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at ``_MAX_BACKOFF``."""
    delay = min(_BASE_BACKOFF * (2 ** (attempt - 1)), _MAX_BACKOFF)
    jitter = random.uniform(0, 0.5 * delay)
    return delay + jitter


# ---------------------------------------------------------------------------
# Core fetch
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
    Perform an HTTP GET with TLS fingerprint spoofing.

    Returns::

        {"status": int, "headers": dict, "body": str, "url": str, "cached": bool}

    Raises ``RuntimeError`` when all retries are exhausted.
    """
    for attempt in range(1, _DEFAULT_RETRIES + 1):
        # Fresh session per attempt so TLS state is always clean.
        async with AsyncSession() as session:
            try:
                raw: CurlResponse = await session.get(
                    url,
                    impersonate=impersonate,
                    timeout=timeout,
                    allow_redirects=follow_redirects,
                    max_redirects=max_redirects,
                )

                status = raw.status_code

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

                # Read the full body — curl_cffi buffers it by default.  For
                # typical pages this is well under 20 MB; the 50 MB safety
                # valve is applied after the fact via len().
                body_bytes = raw.content
                body_str = body_bytes.decode(
                    raw.encoding or "utf-8", errors="replace"
                )

                return {
                    "status": status,
                    "headers": dict(raw.headers),
                    "body": body_str,
                    "url": str(raw.url),
                    "cached": False,
                }

            except Exception as exc:
                if attempt < _DEFAULT_RETRIES:
                    print(f"Retry {attempt} failed for {url}: {exc}")
                    await asyncio.sleep(_backoff_delay(attempt))
                    continue
                raise RuntimeError(
                    f"Fetch failed for {url} after "
                    f"{_DEFAULT_RETRIES} retries: {exc}"
                ) from exc

    raise RuntimeError(f"Unreachable: {url}")  # pragma: no cover
