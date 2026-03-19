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
     │
     ├── Services  ────────────────────────────────────────────────
     │     order_sync        Upsert scraped orders into DB
     │     price_comparison  Fuzzy matching + ProductMatch creation
     │     prediction        ConsumptionPrediction generation
     │     shopping_list     List generation from predictions
     │     cart              Resolve products → store IDs → scraper
     │
     ├── Scrapers  ────────────────────────────────────────────────
     │     ColesScraper      httpx + GraphQL API
     │     WoolworthsScraper httpx + REST/mobile API
     │
     └── Database  ────────────────────────────────────────────────
           SQLAlchemy async ORM
           SQLite (local dev) or PostgreSQL (production)
```

---

## Layer Responsibilities

### Routes

Routes are thin. They parse request parameters, call one service function or scraper method, and return an HTML fragment or SSE stream. No database access from routes directly — all DB work happens in services.

Routes are organised into sub-packages for the larger domains:

- `routes/views/` — one module per page; renders Jinja2 templates
- `routes/api_prices/` — 6 sub-routers: `charts`, `matches`, `products`, `refresh`, `search`, `product_lookup`
- `routes/api_shopping_list/` — 4 sub-routers: `crud`, `items`, `stores`, `candidates`
- Top-level: `api_auth`, `api_orders`, `api_predictions`, `api_cart`

### Services

Business logic lives here. Services receive an `AsyncSession` and domain objects; they read and write the database and call scrapers as needed.

| Service | Responsibility |
|---------|---------------|
| `order_sync.py` | Bulk-upsert `ScrapedOrder` / `ScrapedProduct` objects into `orders`, `order_items`, `products` |
| `price_comparison.py` | Fuzzy-match products across stores; create/manage `ProductMatch` rows; fetch `PriceHistory` |
| `prediction.py` | Analyse `OrderItem` history per product; write `ConsumptionPrediction` rows |
| `shopping_list.py` | Generate draft lists from predictions; handle store assignment and totals |
| `cart.py` | Resolve `ShoppingListItem` → `store_product_id` via `ProductMatch`; call `scraper.add_to_cart()` |
| `product_resolution.py` | Lookup helpers for resolving canonical products to store-specific IDs |
| `data_management.py` | Bulk-delete helpers (purge orders, products, matches, etc.) |

### Scrapers

Both scrapers implement `BaseScraper` and use `httpx.AsyncClient` — there is no Playwright or browser automation. Cookies are loaded from the `store_cookies` DB table at client construction and persisted back after requests that update them.

| Scraper | Protocol | Notes |
|---------|----------|-------|
| `ColesScraper` | GraphQL over HTTPS | Requires `COLES_API_KEY` (`Ocp-Apim-Subscription-Key` header). Uses `coles_queries.py` for GQL strings. |
| `WoolworthsScraper` | REST + mobile API | Uses `prod.mobile-api.woolworths.com.au` with a static mobile API key for some endpoints. |

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

### Database

Async SQLAlchemy with `aiosqlite` driver for local dev and `asyncpg` for PostgreSQL in production. The engine is configured in `database.py` with `pool_size=10, max_overflow=20`. `init_db()` runs at startup (lifespan) to verify connectivity.

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
- Coles: `COLES_PRICE_REFRESH_CONCURRENCY = 20`, no delay
- Woolworths: `WOOLWORTHS_PRICE_REFRESH_CONCURRENCY = 1`, 250ms delay between requests

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
        upsert ConsumptionPrediction (one row per product, unique constraint)
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
| `DATABASE_URL` | SQLAlchemy async connection string (required) |
| `COLES_API_KEY` | Coles GraphQL `Ocp-Apim-Subscription-Key` header |
| `DEBUG` | Enable SQLAlchemy query logging |
| `HOST`, `PORT` | Bind address (default `127.0.0.1:8000`) |
| `DATA_DIR` | Directory for app data files |
| `LOG_DIR` | Directory for rotating log files |

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
| `PREDICTION_LOOKAHEAD_DAYS` | 14 | Include items predicted to run out within N days |
| `PREDICTION_LEAD_TIME_DAYS` | 14 | Also include items already N days overdue |
| `PREDICTION_PURCHASE_COUNT_MIN` | 3 | Minimum purchases before generating a prediction |
| `COLES_PRICE_REFRESH_CONCURRENCY` | 20 | Parallel requests during Coles price refresh |
| `WOOLWORTHS_PRICE_REFRESH_CONCURRENCY` | 1 | Parallel requests during Woolworths refresh |
| `WOOLWORTHS_PRICE_FETCH_DELAY_S` | 0.25 | Delay between Woolworths price requests |

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
- Database: external PostgreSQL (connection string via `DATABASE_URL` env var)

For local development, SQLite works with `DATABASE_URL=sqlite+aiosqlite:///./data/shopping.db`.
