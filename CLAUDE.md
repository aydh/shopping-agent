# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[dev]"

uvicorn shopping_agent.main:app --reload --host 0.0.0.0 --ssl-certfile localhost.pem --ssl-keyfile localhost-key.pem   # Dev server on port 8000 (HTTPS)
uvicorn shopping_agent.main:app --host 0.0.0.0 --ssl-certfile localhost.pem --ssl-keyfile localhost-key.pem            # Production (HTTPS)

pytest                                     # All tests
pytest tests/path/to/test_file.py::test_name  # Single test

ruff check .
mypy .
```

Copy `.env.example` to `.env` before running.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — layers, key data flows, configuration reference, deployment
- [APIS.md](APIS.md) — all API endpoints, parameters, and common workflow chains
- [DATAMODEL.md](DATAMODEL.md) — ER diagram, table schemas, and design decisions

## Architecture

FastAPI app under `src/shopping_agent/`. Frontend is Jinja2 templates + HTMX with Server-Sent Events for streaming.

**Layer overview:**
- `main.py` — App entry point, router registration, lifespan (DB init + APScheduler)
- `config.py` — Settings from env vars; tuning thresholds for matching, predictions, price refresh
- `routes/` — Nested sub-routers; `routes/views/` renders HTML pages; `routes/api_*.py` and sub-routers handle JSON/SSE
- `services/` — Business logic (no direct DB access from routes)
- `scrapers/` — httpx-based API clients for Coles (GraphQL) and Woolworths (REST); no Playwright
- `models/` — SQLAlchemy async ORM (PostgreSQL via asyncpg, hosted on Supabase)
- `cache.py` — Caching utilities
- `db_helpers.py` — Database utility functions
- `templating.py` — Jinja2 template rendering helpers

**Routes structure:**
- `routes/views/` — Per-page view modules: `dashboard.py`, `orders.py`, `predictions.py`, `prices.py`, `product_lookup.py`, `settings.py`, `shopping_list.py`
- `routes/api_prices/` — Sub-routers: `charts.py`, `matches.py`, `products.py`, `refresh.py`, `search.py`, `product_lookup.py`
- `routes/api_shopping_list/` — Sub-routers: `crud.py`, `items.py`, `stores.py`, `candidates.py`
- Top-level: `api_auth.py`, `api_cart.py`, `api_orders.py`, `api_predictions.py`

**Key flows:**

1. **Order sync** (`api_orders.py` → `order_sync.py`): SSE stream that calls the httpx scraper to fetch order history, upserts `Order`/`OrderItem`/`Product` rows, then runs auto product matching.

2. **Price comparison** (`routes/api_prices/` → `services/price_comparison.py`): Fuzzy-matches products across stores using rapidfuzz. `ProductMatch` records store Coles↔Woolworths equivalents with a confidence score. `find_or_create_match()` checks DB first, then falls back to local fuzzy matching, then scraper search.

3. **Shopping list** (`routes/api_shopping_list/` → `services/shopping_list.py`): Draft lists are generated from `ConsumptionPrediction` rows or built manually. Confirming a list triggers cart addition via the scraper.

4. **Cart** (`api_cart.py` → `services/cart.py`): Resolves canonical products to store-specific IDs via `ProductMatch`, then calls `scraper.add_to_cart()`.

5. **Predictions** (`api_predictions.py` → `services/prediction.py`): Analyzes `OrderItem` purchase intervals and quantities to generate `ConsumptionPrediction` rows.

**Scrapers** (`scrapers/base.py`, `coles.py`, `woolworths.py`, `coles_queries.py`): Both scrapers use `httpx.AsyncClient` — Coles via GraphQL API, Woolworths via REST API. No Playwright/browser automation. `BaseScraper` defines the abstract interface: `get_order_history()`, `search_product()`, `add_to_cart()`, `import_cookies()`. `StoreCookies` model persists auth sessions.

**Models:** `Product` (store-specific) → `ProductMatch` (cross-store equivalency) → `PriceHistory`. `Order` + `OrderItem` tracks purchase history. `ShoppingList` + `ShoppingListItem` for active lists. `ConsumptionPrediction` for runout forecasts. `StoreCookies` persists auth sessions.
