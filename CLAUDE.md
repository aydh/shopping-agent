# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[dev]"

uvicorn shopping_agent.main:app --reload   # Dev server on port 8000
uvicorn shopping_agent.main:app            # Production

pytest                                     # All tests
pytest tests/path/to/test_file.py::test_name  # Single test

ruff check .
mypy .
```

Copy `.env.example` to `.env` before running. The only required config is the data directory path — all other settings have sensible defaults.

## Architecture

FastAPI app under `src/shopping_agent/`. The database is SQLite via SQLAlchemy async (aiosqlite). There are no migrations — `init_db()` runs `create_all` on startup.

### Request flow

1. Browser → Jinja2 HTML templates (Tailwind CSS + HTMX for partial updates, SSE for streaming)
2. `routes/views.py` — full-page HTML renders
3. `routes/api_*.py` — HTMX fragment endpoints returning `HTMLResponse` snippets (not JSON)
4. `services/` — business logic (pure async functions, receive `AsyncSession` as arg)
5. `scrapers/` — Playwright-based scrapers for Coles and Woolworths

### Authentication

Both stores authenticate via cookie import only — no programmatic login. The user pastes a JSON cookie array from the Cookie-Editor browser extension. Cookies are stored as JSON files in `data/cookies/{store}.json`. `BrowserManager` (singleton in `scrapers/browser_manager.py`) manages a shared headless Chromium instance with per-store browser contexts that load these cookies.

### Data model

- `Product` — store-scoped product with `store_product_id` unique per store
- `Order` / `OrderItem` — scraped order history; `store_order_id` is the unique key
- `PriceHistory` — one row per product per order date, populated during order sync
- `ProductMatch` — cross-store product pairs (Coles ↔ Woolworths), with fuzzy-match confidence; `is_confirmed` marks user-verified matches
- `ConsumptionPrediction` — one row per canonical product, computed by `services/prediction.py`
- `ShoppingList` / `ShoppingListItem` — active shopping list with per-item store choice

### Key service logic

**Order sync** (`services/order_sync.py`): Upserts scraped orders and products; records price history from `price_paid` per order date.

**Price comparison** (`services/price_comparison.py`): Matches products across stores using `rapidfuzz` token sort/set ratio on normalized names, with a size-compatibility adjustment (+15 if sizes match, −20 if they differ). Matches are stored in `ProductMatch` and reused on subsequent calls.

**Consumption prediction** (`services/prediction.py`): `compute_prediction()` calculates exponentially-weighted daily consumption from inter-purchase intervals, then estimates runout date and recommended next purchase date. Predictions are grouped by `ProductMatch` so cross-store purchases of the same item are treated as one product. `generate_candidates()` filters predictions into shopping list suggestions within a configurable lookahead window.

### Scrapers

`BaseScraper` (abstract, `scrapers/base.py`) defines the interface. `coles_scraper` and `woolworths_scraper` are module-level singletons. Each scraper uses `BrowserManager.get_context(store)` to get a browser context with pre-loaded cookies.

### Templates

Jinja2 templates under `src/shopping_agent/templates/`. `base.html` provides the nav shell. Partial templates prefixed with `_` (e.g. `_predictions_grid.html`) are rendered by HTMX fragment endpoints for in-place updates. All styling is Tailwind CSS via CDN; no build step.
