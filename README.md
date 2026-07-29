# Ultra Scraper — Backend

A tiny, fast FastAPI wrapper around [Scrapling](https://github.com/d4vinci/Scrapling) that turns a giant scraping framework into a simple JSON API:

```http
POST /scrape
{ "url": "...", "selectors": ["h1", ".price"] }
```

> **Frontend repo / live UI:** https://github.com/SaddexRnx/uscraper-frontend — hosted at **https://uscraper.lovable.app**
> **Upstream library:** https://github.com/d4vinci/Scrapling

---

## The idea in one paragraph

Scrapling is ~50,000 lines of code — a full scraping framework that builds *everything* from scratch: its own HTTP client, TLS/JA3 fingerprinting via `curl_cffi`, a headless-browser automation layer (CDP wrappers), fingerprint generation (WebGL, canvas, audio, fonts, WebRTC), a Cloudflare challenge solver, CSS/XPath engines, a response cache, a rate limiter and a proxy rotator. **Ultra Scraper is ~500 lines** that import the right pieces of Scrapling and chain them together into a single, resilient `/scrape` endpoint. You get all the heavy lifting for free; the small surface area makes it *fast* to run and *easy* to reason about.

---

## The fallback chain

Every request tries increasingly heavy methods until one succeeds. The winning tier is returned as `method_used` so the frontend can badge it.

```text
POST /scrape
      │
      ▼
┌──────────────────────────────────────────────────────────┐
│ 1.  curl_cffi                                            │
│     Impersonates a real Chrome TLS/JA3 fingerprint.      │
│     Cheapest, fastest. Beats basic WAFs.                 │
│     method_used = "curl_cffi"                            │
└──────────────────────────────────────────────────────────┘
      │ blocked / challenge detected
      ▼
┌──────────────────────────────────────────────────────────┐
│ 2.  AsyncStealthySession  (Scrapling)                    │
│     Real headless Chromium via patchright, with          │
│     BrowserForge fingerprints, patched navigator.        │
│     webdriver / chrome.runtime / plugins, and a          │
│     Cloudflare challenge waiter.                         │
│     method_used = "stealthy_fallback"                    │
└──────────────────────────────────────────────────────────┘
      │ still blocked
      ▼
┌──────────────────────────────────────────────────────────┐
│ 3.  Paid proxy fallback (BYOK)                           │
│     ScraperAPI / ZenRows / ScrapingBee if a key is set.  │
│     method_used = "<provider>"                           │
└──────────────────────────────────────────────────────────┘
```

That orchestration logic is the entire value of this repo — everything below it lives inside Scrapling.

---

## What each piece does

### `main.py` — the FastAPI app (~200 lines)

- Defines `POST /scrape`, `GET /history`, `GET /health`.
- Validates the incoming JSON (`url`, `selectors: list[str]`).
- Applies auth: `Authorization: Bearer <token>` → looks up quota and admin flag.
- Calls `ultra_stealth_fetcher.fetch(url)` to get the DOM.
- Runs each CSS selector against the returned page and packs the result into:

  ```json
  {
    "url": "...",
    "status": 200,
    "cached": false,
    "method_used": "curl_cffi",
    "data": { "h1": ["..."], ".price": ["..."] }
  }
  ```

- Persists a small history row (url, selectors, method, timestamp) for `/history`.

### `ultra_stealth_fetcher.py` — the orchestrator (~200 lines)

- Tier 1: `curl_cffi.requests.get(..., impersonate="chrome124")`.
- Tier 2: `AsyncStealthySession()` from Scrapling — a real Chromium with fingerprint spoofing and a Cloudflare waiter baked in.
- Tier 3: proxy fallback — forwards the URL to whichever paid provider the caller configured.
- Decides "blocked vs OK" from status code, response length, and known challenge markers (`cf-mitigated`, `cf-browser-verification`, Datadome/Perimeterx signals).
- Returns a normalized `(status, html, method_used)` tuple so `main.py` doesn't care which tier answered.

### `cache.py` — response cache (~50 lines)

- Small on-disk LRU keyed by `(url, sorted(selectors))`.
- Skipped for non-idempotent status codes.
- Sets `cached: true` in the response when a hit is served.

### `auth.py` — tokens & quota (~50 lines)

- Loads a token table (SQLite or a JSON file).
- Tokens starting with `ADMIN-` unlock `/admin/*` (token generation, quota edits).
- Every successful scrape increments the caller's `quota_used`.

Everything else — TCP fingerprint spoofing, TLS negotiation, headless browser control, JS challenge execution, canvas/WebGL fingerprinting — lives inside Scrapling. This project just picks the right knob and turns it.

---

## API reference

### `POST /scrape`

**Request**

```json
{
  "url": "https://example.com/product",
  "selectors": ["h1", ".price", ".stock-status"]
}
```

Headers: `Authorization: Bearer <token>` (required), `Content-Type: application/json`.

**Response — 200**

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

**Errors**

| Status | Meaning |
| --- | --- |
| 400 | Malformed JSON, missing `url`, or invalid selector list |
| 401 | Missing / invalid bearer token |
| 402 | Quota exceeded |
| 422 | All tiers failed to reach the target |
| 5xx | Internal error — check server logs |

### `GET /history`

Returns the recent scrapes belonging to the authenticated token.

### `GET /health`

Cheap liveness probe — returns `{"ok": true}`.

---

## Running the backend

### Requirements

- Python 3.11+
- A working Chromium (installed automatically by `patchright install chromium`)

### Install

```sh
git clone https://github.com/SaddexRnx/uscraper.git
cd uscraper
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
patchright install chromium
```

### Configure

`.env`:

```dotenv
HOST=0.0.0.0
PORT=8000
CACHE_TTL_SECONDS=900

# Optional BYOK fallbacks (any of these enables Tier 3)
SCRAPERAPI_KEY=
ZENROWS_KEY=
SCRAPINGBEE_KEY=
```

### Run

```sh
uvicorn main:app --host 0.0.0.0 --port 8000
```

The frontend at https://uscraper.lovable.app connects by default to `https://uscraper.duckdns.org` — override the Backend API URL from the **Settings** page (gear icon) to point at your instance.

### Docker

```sh
docker build -t uscraper .
docker run -p 8000:8000 --env-file .env uscraper
```

---

## Why so small?

Because every hard problem is already solved in Scrapling:

- **TLS/JA3 spoofing** → `curl_cffi` (bundled through Scrapling).
- **Real browser stealth** → `AsyncStealthySession` (patched Chromium + BrowserForge fingerprints).
- **Cloudflare challenges** → Scrapling waits for `cf-browser-verification` and extracts the clean DOM.
- **CSS / XPath** → Scrapling's built-in parser.
- **Cache / rate limit / proxy rotation** → also inside Scrapling.

Ultra Scraper is the mechanic; Scrapling is the factory. That's why 500 lines can do the work of 50,000 — and why it stays fast.

---

## Credits

- Upstream: [Scrapling by d4vinci](https://github.com/d4vinci/Scrapling) — the actual heavy lifting.
- Frontend: [uscraper-frontend](https://github.com/SaddexRnx/uscraper-frontend) — live at https://uscraper.lovable.app.
- Contact: Telegram **[@Saddex_x](https://t.me/Saddex_x)**.
