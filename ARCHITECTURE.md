# ARCHITECTURE.md

Architecture reference for the shopping agent — a personal grocery automation tool that syncs order history from Coles and Woolworths, predicts what you'll need to buy, and adds items to your cart.

---

## High-Level Overview

```
Browser (HTMX)
     │  GET/POST HTML fragments
     │  SSE streams
     ▼
FastAPI app (Python 3.12)
     │
     ├── Routes  ──────────────────────────────────────────────────
     │     views/          Full-page renders (Jinja2)
     │     api_orders       SSE order sync
     │     api_prices/      Price comparison, matching, refresh
     │     api_shopping_list/ List lifecycle, items, store assignment
     │     api_cart         Cart add (SSE)
     │     api_auth         Cookie import/validate/logout
     │     api_predictions  Prediction refresh
     │     mcp              FastMCP server (19 tools, mounted at /mcp)
     │
     ├── Services  ────────────────────────────────────────────────
     │     order_sync        Upsert scraped orders into DB
     │     price_comparison  Fuzzy matching + ProductMatch creation
     │     price_refresh     Concurrent price fetching + PriceHistory
     │     prediction        ConsumptionPrediction generation
     │     shopping_list     List generation from predictions
     │     cart              Resolve products → store IDs → scraper
     │     product_resolution Lookup helpers for cross-store product IDs
     │     data_management   Bulk-delete helpers
     │
     ├── Scrapers  ────────────────────────────────────────────────
     │     ColesScraper      httpx + GraphQL API
     │     WoolworthsScraper httpx + REST/mobile API
     │     registry          Singleton + per-user scraper instances
     │
     ├── Auth  ────────────────────────────────────────────────────
     │     auth.py           Supabase JWT verification, CurrentUser
     │     database.py       RLS claim injection per request
     │
     └── Database  ────────────────────────────────────────────────
           SQLAlchemy async ORM
           PostgreSQL via asyncpg (Supabase)
```

---

## Layer Responsibilities

### Routes

Routes are thin. They parse request parameters, call one service function or scraper method, and return an HTML fragment or SSE stream. No database access from routes directly — all DB work happens in services.

Routes are organised into sub-packages for the larger domains:

- `routes/views/` — one module per page; renders Jinja2 templates
  - Pages: `dashboard`, `login`, `register`, `auth_callback`, `oauth_consent`, `health`, `orders`, `predictions`, `prices`, `product_lookup`, `shopping_list`, `settings`
- `routes/api_prices/` — 6 sub-routers: `charts`, `matches`, `products`, `refresh`, `search`, `product_lookup`
- `routes/api_shopping_list/` — 4 sub-routers: `crud`, `items`, `stores`, `candidates`
- Top-level: `api_auth`, `api_orders`, `api_predictions`, `api_cart`
- `routes/mcp.py` — FastMCP server with 19 tools for LLM agent access, mounted at `/mcp`

### Services

Business logic lives here. Services receive an `AsyncSession` and domain objects; they read and write the database and call scrapers as needed.

| Service | Responsibility |
|---------|---------------|
| `order_sync.py` | Bulk-upsert `ScrapedOrder` / `ScrapedProduct` objects into `orders`, `order_items`, `products` |
| `price_comparison.py` | Fuzzy-match products across stores; create/manage `ProductMatch` rows; fetch `PriceHistory` |
| `price_refresh.py` | Concurrent price fetch for all/visible products; update `Product.current_price`; append `PriceHistory` rows; sync active shopping list prices |
| `prediction.py` | Analyse `OrderItem` history per product; write `ConsumptionPrediction` rows |
| `shopping_list.py` | Generate draft lists from predictions; handle store assignment and totals |
| `cart.py` | Resolve `ShoppingListItem` → `store_product_id` via `ProductMatch`; call `scraper.add_to_cart()` |
| `product_resolution.py` | Lookup helpers for resolving canonical products to store-specific IDs via `ProductMatch` |
| `data_management.py` | Bulk-delete helpers (purge orders, products, matches, predictions, etc.) |

### Scrapers

Both scrapers implement `BaseScraper` and use `httpx.AsyncClient`. Playwright is used only for interactive browser-based login (experimental feature). Cookies are loaded from the `store_cookies` DB table at client construction and persisted back after requests that update them.

| Scraper | Protocol | Notes |
|---------|----------|-------|
| `ColesScraper` | GraphQL over HTTPS | Requires `COLES_API_KEY` (`Ocp-Apim-Subscription-Key` header). Uses `coles_queries.py` for GQL strings. |
| `WoolworthsScraper` | REST + mobile API | Uses `prod.mobile-api.woolworths.com.au` with a static mobile API key. Auto-fetches Akamai `_abck` cookie from homepage on first use. Manages JWT auth token with auto-refresh. |

**`BaseScraper` interface:**

```python
async def is_authenticated() -> bool
async def get_order_history(limit) -> list[ScrapedOrder]
async def stream_order_history(limit) -> AsyncGenerator[ScrapedOrder]
async def search_product(query) -> list[ScrapedProduct]
async def get_product_price(store_product_id, product_name) -> ScrapedProduct | None
async def add_to_cart(items: list[tuple[str, int]]) -> dict[str, bool]
async def get_cart_url() -> str
async def import_cookies(cookie_json: str) -> bool
async def logout() -> None
```

**Cookie flow:** User exports cookies from their browser (DevTools or Cookie-Editor extension) → `POST /api/auth/import-cookies/{store}` → stored as JSON in `store_cookies` table → loaded into `httpx.Cookies` jar on each scraper client creation.

**Scraper registry** (`scrapers/registry.py`): Provides global singletons (`coles_scraper`, `woolworths_scraper`) for background tasks and per-user instances via `get_scraper(user_id, store)` for authenticated request context.

### Auth

Authentication is handled by Supabase. All HTML pages use cookie-based auth; API routes use Bearer tokens.

- `auth.py` — `CurrentUser` dataclass (user_id, email, raw_claims). Token decoding with caching:
  - HS256: shared secret (`SUPABASE_JWT_SECRET`)
  - RS256/ES256: JWKS endpoint with fallback to `/auth/v1/user`
  - 5-minute token cache, 1-hour JWKS cache
  - `get_current_user()` — FastAPI dependency for API routes (Bearer header)
  - `get_current_user_from_cookie()` — FastAPI dependency for HTML pages (cookie; redirects to `/login` on failure)

- `database.py` — Three session types:
  - `get_session()` — plain session, no RLS (for background tasks and scheduler)
  - `get_user_session()` — RLS-injecting via Bearer token
  - `get_user_session_from_cookie()` — RLS-injecting via cookie
  - `set_rls_claims()` — injects Supabase JWT claims as PostgreSQL GUC (`request.jwt.claims`, `ROLE authenticated`) so Supabase RLS policies on all tables are enforced

### Database

Async SQLAlchemy with `asyncpg` driver for PostgreSQL (Supabase). The engine is configured in `database.py` with `pool_size=10, max_overflow=20, pool_pre_ping=True`. `init_db()` runs at startup (lifespan) to verify connectivity.

Migrations are managed with Alembic (`alembic.ini`). Schema is **not** created by SQLAlchemy `create_all` — use `alembic upgrade head`.

---

## Frontend Architecture

The UI is server-rendered HTML with progressive enhancement via HTMX. There is no JavaScript framework.

- **Jinja2 templates** under `src/shopping_agent/templates/`
- **HTMX** handles all dynamic interactions: inline swaps, form submissions, polling, SSE
- **SSE** (Server-Sent Events) drives the order sync and cart-add flows — the browser connects to a streaming endpoint and rows appear one at a time as the scraper fetches them
- Static assets (`static/css/app.css`, `static/js/app.js`) are minimal

Template organisation:
- `*.html` — full pages
- `_*.html` — partial fragments returned by HTMX requests
- `fragments/` — smaller inline fragments (e.g. a single table row)
- `partials/` — reusable partial layouts

---

## MCP Integration

The app embeds a [FastMCP](https://github.com/jlowin/fastmcp) server mounted at `/mcp`. This exposes 19 tools for LLM agents to interact with grocery automation without a browser.

**MCP tools (read-only):** `get_auth_status`, `get_predictions`, `get_shopping_list`, `get_shopping_list_history`, `search_products`, `get_price_comparison`

**MCP tools (list management):** `create_shopping_list`, `add_item_to_shopping_list`, `update_list_item_quantity`, `remove_list_item`, `assign_cheapest_store_to_all`, `confirm_shopping_list`, `close_shopping_list`

**MCP tools (data sync):** `sync_orders`, `refresh_prices`, `refresh_predictions`

**MCP tools (product matching):** `match_products`, `find_product_match`, `confirm_product_match`

Authentication: when `SUPABASE_URL` is set, the MCP server uses `SupabaseProvider` (OAuth 2.0 with PKCE). Discovery endpoints (`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`) are registered at app root. A fallback `/authorize` redirect is also registered.

---

## Key Data Flows

### 1. Order Sync (SSE)

```
Browser opens SSE connection
  → GET /api/orders/sync-stream/{store}
  → Route creates scraper instance
  → scraper.stream_order_history() yields ScrapedOrder one at a time
  → order_sync.sync_orders() upserts Order + OrderItem + Product rows
  → Route SSE-emits rendered HTML row to browser
  → Stream closes when done
```

### 2. Price Matching

```
POST /api/prices/match-products
  → price_comparison.match_all_products()
  → For each (coles_product, woolworths_product) pair:
      normalize_product_name() on both
      rapidfuzz token_sort_ratio + token_set_ratio → score
      apply SIZE_MATCH_BONUS / SIZE_MISMATCH_PENALTY
      apply BRAND_MATCH_THRESHOLD gate
      score >= FUZZY_MATCH_THRESHOLD → create ProductMatch(method="fuzzy")
```

For products that can't be auto-matched:
```
GET /api/prices/search-match/{product_id}?q=<term>
  → scraper.search_product(q) on the opposite store
  → results scored against FUZZY_SEARCH_THRESHOLD
  → user picks result
POST /api/prices/search-match/confirm
  → upsert Product from search result
  → create ProductMatch(method="search")
```

### 3. Price Refresh

```
POST /api/prices/refresh/{store}
  → background task: for each product of this store:
      scraper.get_product_price(store_product_id)
      update Product.current_price, is_available
      append PriceHistory row
  → GET /api/prices/refresh-progress/{store} (HTMX polling)
      returns progress fraction → browser updates progress bar
```

Concurrency is controlled by module-level constants in `config.py`:
- Coles: `COLES_PRICE_REFRESH_CONCURRENCY = 5`, no delay
- Woolworths: `WOOLWORTHS_PRICE_REFRESH_CONCURRENCY = 2`, 150ms delay + up to 50ms jitter

Scheduled refresh runs every `PRICE_REFRESH_INTERVAL_HOURS` hours (with ±`PRICE_REFRESH_JITTER_MINUTES` jitter) when `ENABLE_SCHEDULER=true`.

### 4. Prediction Generation

```
POST /api/predictions/refresh
  → prediction.refresh_all()
  → For each product with >= PREDICTION_PURCHASE_COUNT_MIN orders
      in the last PRODUCT_RECENCY_DAYS days:
        compute avg_purchase_interval_days from order dates
        compute avg_quantity_per_purchase
        confidence_score = f(purchase_count, interval_variance)
        predicted_runout_date = last_purchased + avg_interval
        upsert ConsumptionPrediction (unique per user_id + product_id)
```

### 5. Shopping List Generation

```
POST /api/shopping-list/generate
  → shopping_list.generate_from_predictions()
  → select predictions where:
      confidence >= MIN_PREDICTION_CONFIDENCE
      predicted_runout_date within [today - PREDICTION_LEAD_TIME_DAYS,
                                     today + PREDICTION_LOOKAHEAD_DAYS]
  → create ShoppingList(status=DRAFT)
  → for each prediction: create ShoppingListItem with prices from ProductMatch
```

### 6. Cart Add (SSE)

```
Browser opens SSE connection
  → GET /api/cart/stream/{store}
  → cart.resolve_items(store) → list of (store_product_id, quantity)
      uses ProductMatch to find store-specific ID for each item
  → scraper.add_to_cart(items)
  → SSE-emit result per item (success/failure + product name)
  → update ShoppingListItem.is_ordered = true on success
```

---

## Configuration

All runtime config is in `config.py` via `pydantic-settings` (`Settings` class reads from `.env`):

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | SQLAlchemy async connection string (required; `postgresql+asyncpg://...`) |
| `COLES_API_KEY` | Coles GraphQL `Ocp-Apim-Subscription-Key` header |
| `WOOLWORTHS_API_KEY` | Woolworths mobile API key |
| `SUPABASE_JWT_SECRET` | Supabase JWT shared secret (for HS256 token verification) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Supabase anon/public key |
| `BASE_URL` | Public HTTPS URL of this app (default `https://localhost:8000`) |
| `MCP_OAUTH_CLIENT_ID` | OAuth client ID registered with Supabase for MCP |
| `MCP_OAUTH_CLIENT_SECRET` | OAuth client secret for MCP |
| `MCP_JWT_ALGORITHM` | JWT algorithm Supabase uses (`ES256`/`RS256` use JWKS; `HS256` uses secret) |
| `ENABLE_SCHEDULER` | Enable scheduled price refresh via APScheduler (default `false`) |
| `SSL_CERTFILE` / `SSL_KEYFILE` | Paths to SSL cert/key for HTTPS |
| `DEBUG` | Enable SQLAlchemy query logging |
| `HOST`, `PORT` | Bind address (default `127.0.0.1:8000`) |
| `DATA_DIR` | Directory for app data files |
| `LOG_DIR` | Directory for rotating log files |
| `PLAYWRIGHT_PROFILE_DIR` | Persistent Chrome profile dir for Playwright interactive login |
| `PLAYWRIGHT_HEADLESS` | Run Playwright in headless mode (default `true`) |
| `PLAYWRIGHT_CHANNEL` | Playwright browser channel (e.g. `"chrome"`) |

Tuning constants (not from env, edit `config.py`):

| Constant | Default | Controls |
|----------|---------|---------|
| `FUZZY_MATCH_THRESHOLD` | 80.0 | Minimum score for local auto-matching |
| `FUZZY_SEARCH_THRESHOLD` | 65.0 | Minimum score for search-result matching |
| `SIZE_MATCH_BONUS` | +25 | Score bonus when unit sizes agree |
| `SIZE_MISMATCH_PENALTY` | -30 | Score penalty when unit sizes conflict |
| `BRAND_MATCH_THRESHOLD` | 70.0 | Hard gate: skip candidates with different brand |
| `MIN_MATCH_CONFIDENCE` | 0.4 | Floor used in price comparison queries |
| `PRODUCT_RECENCY_DAYS` | 365 | Order history window for predictions |
| `MIN_PREDICTION_CONFIDENCE` | 0.4 | Exclude low-confidence predictions from lists |
| `PREDICTION_LOOKAHEAD_DAYS` | 7 | Include items predicted to run out within N days |
| `PREDICTION_LEAD_TIME_DAYS` | 7 | Also include items already N days overdue |
| `PREDICTION_PURCHASE_COUNT_MIN` | 3 | Minimum purchases before generating a prediction |
| `COLES_PRICE_REFRESH_CONCURRENCY` | 5 | Parallel requests during Coles price refresh |
| `COLES_PRICE_FETCH_DELAY_S` | 0.0 | Delay between individual Coles price requests |
| `WOOLWORTHS_PRICE_REFRESH_CONCURRENCY` | 2 | Parallel requests during Woolworths refresh |
| `WOOLWORTHS_PRICE_FETCH_DELAY_S` | 0.15 | Delay between Woolworths price requests |
| `WOOLWORTHS_PRICE_FETCH_JITTER_S` | 0.05 | Max random jitter added on top of Woolworths delay |
| `PRICE_REFRESH_INTERVAL_HOURS` | 4 | How often the scheduled refresh runs |
| `PRICE_REFRESH_JITTER_MINUTES` | 60 | Max random offset applied to each scheduled run |

---

## Logging

Configured at startup in `main.py`:

- **Rotating file log**: `logs/shopping_agent.log`, 10 MB per file, 5 backups, DEBUG level
- **Console**: INFO level
- `aiosqlite` and `sqlalchemy.engine` suppressed to WARNING to reduce noise

---

## Deployment

Deployed on [Render](https://render.com) as a Python web service (`render.yaml`):

- Region: Singapore
- Build: `pip install -e .`
- Start: `uvicorn shopping_agent.main:app --host 0.0.0.0 --port $PORT`
- Database: external PostgreSQL via Supabase (connection string via `DATABASE_URL` env var)
