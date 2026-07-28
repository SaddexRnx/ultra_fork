"""
ultra_stealth_fetcher.py
------------------------
Two-tier async fetcher with smart fallback:

  Tier 1 (Primary)   – fast ``curl_cffi`` with Chrome 120 impersonation.
  Tier 2 (Fallback)  – Scrapling's ``StealthyFetcher`` with Playwright-based
                       stealth, Cloudflare / Datadome bypass, and JS rendering.

If the primary response is blocked (403/429/503 or detection keywords), the
fallback is triggered automatically.  The caller receives whichever succeeds
first.
"""

import asyncio
import random
import re
from hashlib import sha256

from curl_cffi.requests import AsyncSession, Response as CurlResponse
from scrapling.fetchers import StealthyFetcher

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BLOCKED_KEYWORDS = [
    "cloudflare",
    "captcha",
    "challenge",
    "access denied",
    "just a moment",
    "attention required",
    "cf-ray",
    "__cf_chl_tk",
]

_RETRYABLE_CODES = {429, 503}  # 403 is now handled as "blocked" → triggers fallback
_DEFAULT_RETRIES = 2
_BASE_BACKOFF = 1.0
_MAX_BACKOFF = 10.0


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
    return delay + random.uniform(0, 0.5 * delay)


def _is_blocked(status: int, body: str) -> bool:
    """Return ``True`` if the response looks like a bot challenge page."""
    if status in (403, 429, 503):
        return True
    body_lower = body.lower()
    return any(kw in body_lower for kw in _BLOCKED_KEYWORDS)


# ---------------------------------------------------------------------------
# Core fetch with smart fallback
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
    Fetch *url*, automatically falling back to a stealth browser if the
    primary ``curl_cffi`` request is blocked.

    Returns::

        {"status": int, "headers": dict, "body": str, "url": str,
         "cached": bool, "method_used": "curl_cffi" | "stealthy_fallback"}

    Raises ``RuntimeError`` when both tiers fail.
    """
    # ------------------------------------------------------------------
    # Tier 1  –  fast curl_cffi
    # ------------------------------------------------------------------
    for attempt in range(1, _DEFAULT_RETRIES + 1):
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
                body_bytes = raw.content
                body_str = body_bytes.decode(
                    raw.encoding or "utf-8", errors="replace"
                )

                # Check if the response is a bot challenge page
                if _is_blocked(status, body_str):
                    print(
                        f"Tier-1 blocked for {url} "
                        f"(status={status}) – falling back to stealth browser"
                    )
                    # Proceed directly to Tier 2 (don't retry curl_cffi)
                    return await _fallback_fetch(url, timeout)

                return {
                    "status": status,
                    "headers": dict(raw.headers),
                    "body": body_str,
                    "url": str(raw.url),
                    "cached": False,
                    "method_used": "curl_cffi",
                }

            except Exception as exc:
                if attempt < _DEFAULT_RETRIES:
                    print(f"Tier-1 retry {attempt} failed for {url}: {exc}")
                    await asyncio.sleep(_backoff_delay(attempt))
                    continue
                # All curl_cffi retries exhausted — try the fallback
                print(
                    f"Tier-1 retries exhausted for {url} – "
                    f"falling back to stealth browser: {exc}"
                )
                return await _fallback_fetch(url, timeout)

    # If we get here, all curl_cffi attempts failed without exception
    # (e.g., status-based retries exhausted) – try fallback
    return await _fallback_fetch(url, timeout)


# ---------------------------------------------------------------------------
# Tier 2  –  Playwright-based stealth fallback
# ---------------------------------------------------------------------------

async def _fallback_fetch(url: str, timeout: float) -> dict:
    """
    Fetch *url* via Scrapling's ``StealthyFetcher.async_fetch``.

    Uses a headless Chromium with stealth patches, Cloudflare challenge
    solving, and resource blocking for speed.
    """
    try:
        response = await StealthyFetcher.async_fetch(
            url,
            headless=True,
            solve_cloudflare=True,
            disable_resources=True,
            network_idle=True,
            timeout=int(timeout * 1000),  # StealthyFetcher expects milliseconds
            google_search=True,
        )

        body_bytes = response.body
        body_str = body_bytes.decode("utf-8", errors="replace")

        print(f"Tier-2 (stealth) succeeded for {url} (status={response.status})")

        return {
            "status": response.status,
            "headers": dict(response.headers),
            "body": body_str,
            "url": str(response.url),
            "cached": False,
            "method_used": "stealthy_fallback",
        }

    except Exception as exc:
        raise RuntimeError(
            f"Both tiers failed for {url}: {exc}"
        ) from exc
