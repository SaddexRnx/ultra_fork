"""
ultra_stealth_fetcher.py
------------------------
Three-tier async fetcher with smart fallback:

  Tier 1 (Primary)   – fast ``curl_cffi`` with multiple impersonation profiles.
  Tier 2 (Fallback)  – Scrapling's ``AsyncStealthySession`` (Chromium-based)
                       with stealth, Cloudflare bypass, and JS rendering.
  Tier 3 (Rescue)    – ScraperAPI residential proxy fallback (requires env var).

If Tier 1 is blocked, Tier 2 is tried.  If Tier 2 also fails, Tier 3 is
triggered automatically.  All errors are caught cleanly.
"""

import asyncio
import os
import random
import urllib.request
import urllib.parse
from hashlib import sha256

from curl_cffi.requests import AsyncSession, Response as CurlResponse
from scrapling.fetchers import AsyncStealthySession

# ---------------------------------------------------------------------------
# API keys (from environment)
# ---------------------------------------------------------------------------

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "")

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

_IMPERSONATIONS = ["chrome120", "chrome110", "safari17"]
_DEFAULT_TIMEOUT = 90.0
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
    delay = min(_BASE_BACKOFF * (2 ** (attempt - 1)), _MAX_BACKOFF)
    return delay + random.uniform(0, 0.5 * delay)


def _is_blocked(status: int, body: str) -> bool:
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
    timeout: float = _DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
    max_redirects: int = 10,
) -> dict:
    """
    Fetch *url*, trying multiple impersonation profiles in Tier 1 before
    falling back to a stealth browser.

    Returns::

        {"status": int, "headers": dict, "body": str, "url": str,
         "cached": bool, "method_used": "curl_cffi" | "stealthy_fallback",
         "impersonation": str | None}

    Raises ``RuntimeError`` when both tiers fail.
    """
    # ------------------------------------------------------------------
    # Tier 1  –  fast curl_cffi with multiple impersonations
    # ------------------------------------------------------------------
    for imp in _IMPERSONATIONS:
        print(f"Tier-1 trying impersonation={imp} for {url}")
        for attempt in range(1, 3):
            async with AsyncSession() as session:
                try:
                    raw: CurlResponse = await session.get(
                        url,
                        impersonate=imp,
                        timeout=timeout,
                        allow_redirects=follow_redirects,
                        max_redirects=max_redirects,
                    )

                    status = raw.status_code
                    body_bytes = raw.content
                    body_str = body_bytes.decode(
                        raw.encoding or "utf-8", errors="replace"
                    )

                    if _is_blocked(status, body_str):
                        print(
                            f"  Tier-1 {imp} blocked (status={status}) "
                            f"– trying next impersonation"
                        )
                        break

                    return {
                        "status": status,
                        "headers": dict(raw.headers),
                        "body": body_str,
                        "url": str(raw.url),
                        "cached": False,
                        "method_used": "curl_cffi",
                        "impersonation": imp,
                    }

                except Exception as exc:
                    if attempt < 2:
                        print(f"  Tier-1 {imp} retry {attempt} failed: {exc}")
                        await asyncio.sleep(_backoff_delay(attempt))
                        continue
                    print(f"  Tier-1 {imp} exhausted: {exc}")
                    break

    # All impersonations failed – try stealth
    return await _fallback_fetch(url, timeout)


# ---------------------------------------------------------------------------
# Tier 2  –  Chromium-based stealth fallback  (crash-proof)
# ---------------------------------------------------------------------------

async def _fallback_fetch(url: str, timeout: float) -> dict:
    """
    Fetch *url* via Scrapling's ``AsyncStealthySession`` (Chromium).

    Crash-proof: every operation is wrapped in try/except.  On failure
    a clean ``RuntimeError`` is raised so the FastAPI endpoint never 500s.
    """
    last_exc = None

    for attempt in range(1, 3):
        cfg = dict(
            headless=True,
            solve_cloudflare=True,
            disable_resources=True,
            network_idle=True,
            timeout=int(timeout * 1000),
            google_search=True,
            extra_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        if attempt == 2:
            cfg["network_idle"] = False

        try:
            async with AsyncStealthySession(**cfg) as engine:
                response = await engine.fetch(url)
                status = response.status
                body_str = response.body.decode("utf-8", errors="replace")
                headers = dict(response.headers)

            print(f"Tier-2 succeeded for {url} (status={status})")
            return {
                "status": status,
                "headers": headers,
                "body": body_str,
                "url": str(response.url),
                "cached": False,
                "method_used": "stealthy_fallback",
                "impersonation": None,
            }

        except Exception as exc:
            last_exc = exc
            print(f"  Tier-2 attempt {attempt} failed: {exc}")

    # ------------------------------------------------------------------
    # Tier 3  –  ScraperAPI rescue fallback
    # ------------------------------------------------------------------
    return await _scraperapi_fetch(url, last_exc)


# ---------------------------------------------------------------------------
# Tier 3  –  ScraperAPI (also callable directly from main.py)
# ---------------------------------------------------------------------------

async def _scraperapi_fetch(url: str, previous_error: Exception | None = None) -> dict:
    """Call ScraperAPI with ``render=true``.  Raises RuntimeError on failure."""
    if not SCRAPER_API_KEY:
        raise RuntimeError(f"Tier 2 stealth browser failed: {previous_error}")

    print(f"🔄 Tier 3: Using ScraperAPI residential fallback for {url} (this may take up to 60s)...")
    try:
        params = urllib.parse.urlencode({
            "api_key": SCRAPER_API_KEY,
            "url": url,
            "render": "true",
        })
        api_url = f"https://api.scraperapi.com?{params}"
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
            body_str = resp.read().decode("utf-8", errors="replace")

        if status == 200:
            print(f"  Tier-3 succeeded for {url} (status={status})")
            return {
                "status": status,
                "headers": dict(resp.headers),
                "body": body_str,
                "url": url,
                "cached": False,
                "method_used": "scraperapi_rescue",
                "impersonation": None,
            }

        print(f"  Tier-3 returned non-200 status={status}")
        raise RuntimeError(f"ScraperAPI returned status {status}")

    except Exception as exc:
        print(f"  Tier-3 failed: {exc}")
        raise RuntimeError(f"All 3 tiers failed for {url}: {exc}") from exc
