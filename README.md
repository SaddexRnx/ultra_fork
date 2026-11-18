# Ultra Scraper (backend)

A small FastAPI service that wraps [Scrapling](https://github.com/d4vinci/Scrapling) and exposes it as a simple JSON API:

```http
POST /scrape
{ "url": "...", "selectors": ["h1", ".price"] }
```

The frontend repo is [here](https://github.com/SaddexRnx/uscraper), live at https://uscraper.duckdns.org/.

## About

I wanted a small, readable layer over Scrapling that I could reason about in one sitting. That's what this is: the orchestration without the ceremony. I'm Saddek, a developer in Algeria. More of my work is at [saddexrnx.github.io](https://saddexrnx.github.io), and you can reach me on [Telegram](https://t.me/Saddex_x).

## The idea

Scrapling is a full scraping framework, around 50,000 lines of code, with its own HTTP client, TLS and JA3 fingerprinting, headless browser control, fingerprint generation, a Cloudflare solver, CSS and XPath engines, caching, rate limiting, and proxy rotation.

Ultra Scraper is roughly 500 lines that pull in the pieces it needs and chain them into one `/scrape` endpoint. Scrapling does the heavy lifting. This repo just keeps the surface small so it is fast to run and easy to follow.

## How a request is handled

Each request tries cheaper methods first and moves to heavier ones only when it gets blocked. The method that worked is returned as `method_used`.

1. **curl_cffi.** Impersonates a real Chrome TLS fingerprint. Fastest option and enough for basic WAFs.
2. **AsyncStealthySession from Scrapling.** A real headless Chromium with BrowserForge fingerprints, patched navigator properties, and a Cloudflare challenge waiter.
3. **Paid proxy fallback.** ScraperAPI, ZenRows, or ScrapingBee, if you supply a key.

## The files

- `main.py` (~200 lines): the FastAPI app. Defines `/scrape`, `/history`, and `/health`, validates input, checks the bearer token and quota, runs each selector, and saves a small history row.
- `ultra_stealth_fetcher.py` (~200 lines): the fallback chain above. Works out whether a response was blocked from the status code, response length, and markers like `cf-mitigated`. Returns a normalized `(status, html, method_used)` tuple.
- `cache.py` (~50 lines): a small on-disk LRU keyed by URL and selectors. Skips non-idempotent status codes.
- `auth.py` (~50 lines): token table and quota tracking. Tokens starting with `ADMIN-` unlock `/admin/*`.

Everything below this, including the TLS spoofing and browser stealth, lives inside Scrapling.

## API

### POST /scrape

Requires `Authorization: Bearer <token>` and a JSON body:

```json
{
  "url": "https://example.com/product",
  "selectors": ["h1", ".price", ".stock-status"]
}
```

A successful response:

```json
{
  "url": "https://example.com/product",
  "status": 200,
  "cached": false,
  "method_used": "curl_cffi",
  "data": {
    "h1": ["Widget Pro"],
    ".price": ["$19.99"],
    ".stock-status": ["In stock"]
  }
}
```

Errors: `400` for bad input, `401` for a missing or invalid token, `402` if the quota is used up, `422` if every tier failed, and `5xx` for internal errors.

### GET /history

Returns recent scrapes for the authenticated token.

### GET /health

Returns `{"ok": true}`.

## Run it

Python 3.11+ and Chromium (installed by `patchright install chromium`).

```sh
git clone https://github.com/SaddexRnx/ultra_fork.git
cd ultra_fork
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
patchright install chromium
```

Create a `.env` file:

```dotenv
HOST=0.0.0.0
PORT=8000
CACHE_TTL_SECONDS=900

# Optional, any one of these enables the proxy fallback
SCRAPERAPI_KEY=
ZENROWS_KEY=
SCRAPINGBEE_KEY=
```

Then:

```sh
uvicorn main:app --host 0.0.0.0 --port 8000
```

The frontend points at `https://uscraper.duckdns.org` by default. Change the Backend API URL in its Settings page to point at your own instance.

With Docker:

```sh
docker build -t uscraper .
docker run -p 8000:8000 --env-file .env uscraper
```

## Credits

[Scrapling by d4vinci](https://github.com/d4vinci/Scrapling) does the actual work. If you find this useful, star that repo too.

Built by Saddex. Portfolio: https://saddexrnx.github.io/. Contact: [@Saddex_x](https://t.me/Saddex_x).
