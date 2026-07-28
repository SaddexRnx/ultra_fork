"""
ultra_stealth_fetcher.py
------------------------
Two-tier async fetcher with smart fallback:

  Tier 1 (Primary)   – fast ``curl_cffi`` with multiple impersonation profiles.
  Tier 2 (Fallback)  – Scrapling's ``StealthyFetcher`` with Playwright-based
                       stealth, Cloudflare / Datadome bypass, and JS rendering.

If the primary response is blocked (403/429/503 or detection keywords), the
fallback is triggered automatically.  The caller receives whichever succeeds
first.
"""

import asyncio
import random
from hashlib import sha256

from curl_cffi.requests import AsyncSession, Response as CurlResponse
from scrapling.fetchers import AsyncStealthySession

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
# Tier 2  –  Playwright-based stealth fallback
# ---------------------------------------------------------------------------

async def _fallback_fetch(url: str, timeout: float) -> dict:
    """
    Fetch *url* via Scrapling's ``AsyncStealthySession`` with human-like
    delays, extra stealth patches, and Cloudflare reload logic.
    """
    last_exc = None
    for attempt in range(1, 3):
        try:
            cfg = dict(
                headless=True,
                solve_cloudflare=True,
                disable_resources=True,
                network_idle=True,
                timeout=int(timeout * 1000),
                google_search=True,
                real_chrome=True,
                extra_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            if attempt == 2:
                cfg["network_idle"] = False
                cfg["headless"] = False

            async with AsyncStealthySession(**cfg) as engine:
                response = await engine.fetch(
                    url,
                    page_setup=_page_setup,
                    page_action=_page_action,
                )

            body_str = response.body.decode("utf-8", errors="replace")
            print(f"Tier-2 succeeded for {url} (status={response.status}, attempt={attempt})")
            return {
                "status": response.status,
                "headers": dict(response.headers),
                "body": body_str,
                "url": str(response.url),
                "cached": False,
                "method_used": "stealthy_fallback",
                "impersonation": None,
            }

        except Exception as exc:
            last_exc = exc
            print(f"  Tier-2 attempt {attempt} failed for {url}: {exc}")

    raise RuntimeError(f"Both tiers failed for {url}: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Page-level callbacks (used inside _fallback_fetch via closure)
# ---------------------------------------------------------------------------

async def _page_setup(page) -> None:
    """Run before navigation: random delay + extra stealth patches."""
    delay = random.uniform(2, 5)
    print(f"  Tier-2 waiting {delay:.1f}s before navigation")
    await asyncio.sleep(delay)
    await page.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', {get: () => false});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        """
    )


async def _page_action(page) -> None:
    """Run after navigation + Cloudflare solve: reload if challenge persists."""
    await asyncio.sleep(random.uniform(1, 3))
    html = await page.content()
    first_500 = html[:500].lower()
    if any(kw in first_500 for kw in ["captcha", "challenge", "cloudflare"]):
        print("  Tier-2 Cloudflare challenge still detected, waiting 5s then reloading...")
        await asyncio.sleep(5)
        await page.reload(wait_until="load")
        await page.wait_for_timeout(3000)
        html_after = await page.content()
        if any(kw in html_after[:200].lower() for kw in ["captcha", "challenge", "cloudflare"]):
            print("  Tier-2 Cloudflare still present after reload")
        else:
            print("  Tier-2 Cloudflare resolved on reload!")
