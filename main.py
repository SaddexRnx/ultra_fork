"""
main.py
-------
FastAPI application that orchestrates the three core modules:

  1. ``ultra_stealth_fetcher``  – TLS-fingerprinted HTTP fetcher
  2. ``smart_adaptor``          – memory-efficient HTML parser & extractor
  3. ``memory_safe_cache``      – SQLite-backed cache + token-bucket rate limiter

Endpoints
---------
POST /scrape
    JSON body: {"url": "...", "selectors": ["..."]}
    Returns extracted text for each selector alongside response metadata.
"""

import asyncio
import logging
import traceback
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

from ultra_stealth_fetcher import UltraStealthFetcher
from smart_adaptor import SmartAdaptor
from memory_safe_cache import ResponseCache, DomainRateLimiter

# ---------------------------------------------------------------------------
# Logging  –  keep it quiet in production
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ultra_scraper")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Ultra Scraper",
    description="Ultra-lightweight scraping API with TLS fingerprinting, "
                "memory-safe parsing, and persistent caching.",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# Shared resources  (lifespan-managed)
# ---------------------------------------------------------------------------

_fetcher: Optional[UltraStealthFetcher] = None
_cache: Optional[ResponseCache] = None
_limiter: Optional[DomainRateLimiter] = None


@app.on_event("startup")
async def startup() -> None:
    global _fetcher, _cache, _limiter
    _fetcher = UltraStealthFetcher(
        impersonate="chrome120",
        max_retries=3,
        base_backoff=1.0,
        max_backoff=30.0,
        timeout=30.0,
    )
    _cache = ResponseCache(ttl=3600)  # uses temp dir by default (writable on Render)
    _limiter = DomainRateLimiter(capacity=10, refill_rate=2.0)
    log.info("Ultra Scraper started – resources initialised.")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ScrapeRequest(BaseModel):
    url: HttpUrl
    selectors: List[str]


class ScrapeResponse(BaseModel):
    url: str
    status: int
    cached: bool
    data: dict  # selector -> list of extracted texts


# ---------------------------------------------------------------------------
# Exception handler  –  never leak HTML tracebacks
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print("🚨 CRITICAL ERROR IN SCRAPING API 🚨")
    print(traceback.format_exc())

    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc) # Temporarily show the real error for debugging
        },
    )


# ---------------------------------------------------------------------------
# Core endpoint
# ---------------------------------------------------------------------------

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_endpoint(body: ScrapeRequest) -> dict:
    """
    Fetch *url*, optionally extract data via CSS/XPath *selectors*.

    The response is cached for 1 hour (TTL).  Repeated requests for the same
    URL within that window are served from the SQLite cache.
    """
    assert _fetcher is not None
    assert _cache is not None
    assert _limiter is not None

    url = str(body.url)

    # ------------------------------------------------------------------
    # 1. Check the persistent cache first
    # ------------------------------------------------------------------
    cached = await _cache.get(url)
    if cached is not None:
        log.info("Cache HIT for %s", url)
        html = cached.body
        status = cached.status
        was_cached = True
    else:
        # ------------------------------------------------------------------
        # 2. Rate-limit check (per domain)
        # ------------------------------------------------------------------
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        wait = _limiter.acquire(domain)
        if wait > 0.0:
            log.info("Rate-limited for %s – sleeping %.2f s", domain, wait)
            await asyncio.sleep(wait)

        # ------------------------------------------------------------------
        # 3. Fetch via stealth fetcher
        # ------------------------------------------------------------------
        result = await _fetcher.fetch(url)
        status = result["status"]
        html = result["body"]

        # Store in cache (even for errors, so we don't hammer failing sites)
        await _cache.set(url, status, result["headers"], html)
        was_cached = False

    # ------------------------------------------------------------------
    # 4. Parse and extract
    # ------------------------------------------------------------------
    extracted: dict = {}
    if body.selectors:
        with SmartAdaptor(html, url=url) as adaptor:
            for sel in body.selectors:
                texts = [adaptor.text(el) for el in adaptor.css(sel)]
                extracted[sel] = texts

    return {
        "url": url,
        "status": status,
        "cached": was_cached,
        "data": extracted,
    }


# ---------------------------------------------------------------------------
# Health-check endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
