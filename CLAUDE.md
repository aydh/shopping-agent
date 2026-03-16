# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[dev]"

uvicorn shopping_agent.main:app --reload --host 0.0.0.0   # Dev server on port 8000
uvicorn shopping_agent.main:app --host 0.0.0.0            # Production

pytest                                     # All tests
pytest tests/path/to/test_file.py::test_name  # Single test

ruff check .
mypy .
```

Copy `.env.example` to `.env` before running.

## Architecture

FastAPI app under `src/shopping_agent/`. Frontend is Jinja2 templates + HTMX with Server-Sent Events for streaming.

**Layer overview:**
- `main.py` — App entry point, router registration, lifespan (DB init + APScheduler)
- `routes/` — `views.py` renders HTML pages; `api_*.py` files handle JSON/SSE endpoints
- `services/` — Business logic (no direct DB access from routes)
- `scrapers/` — Playwright browser automation for Coles and Woolworths
- `models/` — SQLAlchemy async ORM (SQLite via aiosqlite)

**Key flows:**

1. **Order sync** (`api_orders.py` → `order_sync.py`): SSE stream that drives a Playwright scraper to fetch order history, upserts `Order`/`OrderItem`/`Product` rows, then runs auto product matching.

2. **Price comparison** (`api_prices.py` → `services/price_comparison.py`): Fuzzy-matches products across stores using rapidfuzz. `ProductMatch` records store Coles↔Woolworths equivalents with a confidence score. `find_or_create_match()` checks DB first, then falls back to local fuzzy matching, then scraper search.

3. **Shopping list** (`api_shopping_list.py` → `services/shopping_list.py`): Draft lists are generated from `ConsumptionPrediction` rows or built manually. Confirming a list triggers cart addition via the scraper.

4. **Cart** (`api_cart.py` → `services/cart.py`): Resolves canonical products to store-specific IDs via `ProductMatch`, then calls `scraper.add_to_cart()`.

5. **Predictions** (`api_predictions.py` → `services/prediction.py`): Analyzes `OrderItem` purchase intervals and quantities to generate `ConsumptionPrediction` rows.

**Scrapers** (`scrapers/browser_manager.py`, `coles.py`, `woolworths.py`): `BrowserManager` manages a shared Playwright browser with per-store contexts. Cookies are persisted to disk and imported from browser DevTools/Cookie-Editor. `BaseScraper` defines the abstract interface: `get_order_history()`, `search_product()`, `add_to_cart()`, `import_cookies()`.

**Models:** `Product` (store-specific) → `ProductMatch` (cross-store equivalency) → `PriceHistory`. `Order` + `OrderItem` tracks purchase history. `ShoppingList` + `ShoppingListItem` for active lists. `ConsumptionPrediction` for runout forecasts. `StoreCookies` persists auth sessions.
