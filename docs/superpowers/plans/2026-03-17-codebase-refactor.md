# Codebase Refactor Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve code quality, structure, and maintainability of the shopping-agent codebase without changing functionality.

**Architecture:** Work proceeds in six phases from highest-to-lowest priority: security fixes, DRY/utility extraction, error handling, modularity splits, type hints/docs, and template improvements.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Jinja2/HTMX, Playwright, Pydantic Settings

---

## Audit Summary

Issues found (44+ total):

| Category | Count | Priority |
|---|---|---|
| Security (hardcoded API key, plaintext cookies) | 2 | CRITICAL |
| DRY violations (ProductMatch resolution, visibility filters, Store enum) | 5 | HIGH |
| Error handling (bare `except`, broad HTTP errors, weak cookie validation) | 3 | HIGH |
| Modularity (491-line route file, 936-line scraper) | 2 | MEDIUM-HIGH |
| HTML in routes (f-string HTML building, inline JS) | 2 | MEDIUM |
| Type hints missing (~20 functions) | 1 | MEDIUM |
| Docstrings missing on complex functions | 6 | LOW |
| Template accessibility & DRY | 4 | LOW |

> **Scope note:** Phases 1–3 are safe, high-value quick wins. Phase 4 (Modularity) involves larger structural changes. Phases 5–6 are polish. Each phase is independently deployable.

---

## File Map

### New files to create
- `src/shopping_agent/services/product_resolution.py` — shared ProductMatch lookup logic (Phase 2)
- `src/shopping_agent/db_helpers.py` — common query helpers: `get_visible_products()`, `get_active_list()` (Phase 2)
- `src/shopping_agent/routes/api_shopping_list/` — split from monolithic file (Phase 4)
  - `__init__.py`
  - `crud.py` — list create/read/delete
  - `items.py` — item add/remove/update
  - `stores.py` — store selection, price lookup
  - `candidates.py` — candidate generation

### Modified files
- `src/shopping_agent/scrapers/coles.py` — move API key to env (Phase 1)
- `src/shopping_agent/config.py` — add API key and other constants (Phase 1)
- `src/shopping_agent/scrapers/base.py` — stricter cookie validation (Phase 3)
- `src/shopping_agent/routes/api_orders.py` — fix bare except, move HTML to template (Phase 3, 5)
- `src/shopping_agent/routes/api_cart.py` — use product_resolution service (Phase 2)
- `src/shopping_agent/routes/api_prices/products.py` — specific HTTP error handling (Phase 3)
- `src/shopping_agent/routes/api_prices/refresh.py` — use product_resolution (Phase 2)
- `src/shopping_agent/routes/api_shopping_list.py` — use helpers, split into sub-routes (Phase 2, 4)
- `src/shopping_agent/services/cart.py` — use product_resolution service (Phase 2)
- `src/shopping_agent/services/price_comparison.py` — use query helpers (Phase 2)
- `src/shopping_agent/routes/views/*.py` — use query helpers (Phase 2)
- `src/shopping_agent/services/*.py` — add type hints and docstrings (Phase 5)
- `templates/base.html` — accessibility fixes (Phase 6)

---

## Chunk 1: Security & Configuration

### Task 1: Move hardcoded Coles API key to environment variable

**Context:** `scrapers/coles.py:34` has `"Ocp-Apim-Subscription-Key": "eae83861d1cd4de6bb9cd8a2cd6f041e"` hardcoded. Anyone with repo access has API access.

**Files:**
- Modify: `src/shopping_agent/scrapers/coles.py:34`
- Modify: `src/shopping_agent/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Read current config.py and coles.py header**

Read `src/shopping_agent/config.py` and the first 50 lines of `src/shopping_agent/scrapers/coles.py` to understand the current structure.

- [ ] **Step 2: Add COLES_API_KEY to Settings in config.py**

In `config.py`, add to the `Settings` class:
```python
coles_api_key: str = Field(default="", description="Coles Ocp-Apim-Subscription-Key")
```

- [ ] **Step 3: Replace hardcoded key in coles.py**

Find the `"Ocp-Apim-Subscription-Key"` line and replace with:
```python
from ..config import settings
# ...
"Ocp-Apim-Subscription-Key": settings.coles_api_key,
```

- [ ] **Step 4: Add placeholder to .env.example**

Add to `.env.example`:
```
COLES_API_KEY=your_coles_api_key_here
```

- [ ] **Step 5: Verify app still starts**

Run: `cd /Users/andrewsaunders/code/shopping-agent && python -c "from shopping_agent.scrapers.coles import ColesScraper; print('OK')"`
Expected: `OK` with no import errors

- [ ] **Step 6: Add key to .env**

Add actual value (from existing `coles.py` before this change) to local `.env` file.

- [ ] **Step 7: Commit**

```bash
git add src/shopping_agent/scrapers/coles.py src/shopping_agent/config.py .env.example
git commit -m "security: move Coles API key from source code to environment variable"
```

---

### Task 2: Move remaining hardcoded values to config

**Context:** Several magic numbers and values are scattered through the code instead of being in `config.py`.

**Files:**
- Modify: `src/shopping_agent/config.py`
- Modify: `src/shopping_agent/routes/api_prices/refresh.py` (~line 148)

- [ ] **Step 1: Read config.py fully**

Read `src/shopping_agent/config.py` to see current constants.

- [ ] **Step 2: Add HTMX poll interval to config**

Add to `config.py`:
```python
price_refresh_poll_interval_ms: int = Field(default=1000, description="HTMX polling interval for price refresh SSE (ms)")
```

- [ ] **Step 3: Replace hardcoded interval in refresh.py**

In `routes/api_prices/refresh.py`, find the `"every 1s"` string and replace with:
```python
from ..config import settings
# ...
f"every {settings.price_refresh_poll_interval_ms}ms"
```

- [ ] **Step 4: Verify no regressions**

Run: `pytest tests/ -v`
Expected: All existing tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/shopping_agent/config.py src/shopping_agent/routes/api_prices/refresh.py
git commit -m "refactor: move hardcoded HTMX poll interval to settings"
```

---

## Chunk 2: DRY — Extract Shared Utilities

### Task 3: Create db_helpers.py with query helpers

**Context:** `Product.is_hidden == False` is checked in 8+ locations as raw SQL. Store enum conversion is inconsistent (`Store(store)` vs `Store[store.upper()]`). Centralizing these prevents subtle bugs.

**Files:**
- Create: `src/shopping_agent/db_helpers.py`
- Modify: `src/shopping_agent/routes/views/prices.py`
- Modify: `src/shopping_agent/routes/api_prices/refresh.py`
- Modify: `src/shopping_agent/services/prediction.py`

- [ ] **Step 1: Read the duplication locations**

Read lines around `is_hidden` in:
- `src/shopping_agent/routes/views/prices.py` (lines 24-30)
- `src/shopping_agent/routes/api_prices/refresh.py` (line 49)
- `src/shopping_agent/services/prediction.py` (line 132)

- [ ] **Step 2: Write tests for the helpers**

In `tests/test_db_helpers.py`:
```python
import pytest
from shopping_agent.models.product import Product, Store

def test_store_from_string_valid():
    from shopping_agent.db_helpers import store_from_string
    assert store_from_string("coles") == Store.COLES
    assert store_from_string("WOOLWORTHS") == Store.WOOLWORTHS

def test_store_from_string_invalid():
    from shopping_agent.db_helpers import store_from_string
    with pytest.raises(ValueError, match="Unknown store"):
        store_from_string("walmart")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_db_helpers.py -v`
Expected: FAIL with `ModuleNotFoundError` or `ImportError`

- [ ] **Step 4: Create db_helpers.py**

Create `src/shopping_agent/db_helpers.py`:
```python
"""Shared SQLAlchemy query helpers and store enum utilities."""
from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models.product import Product, Store


def store_from_string(value: str) -> Store:
    """Convert a string to a Store enum, case-insensitively.

    Args:
        value: Store name string (e.g. "coles", "WOOLWORTHS").

    Returns:
        The matching Store enum value.

    Raises:
        ValueError: If the string does not match any Store.
    """
    try:
        return Store[value.upper()]
    except KeyError:
        valid = [s.value for s in Store]
        raise ValueError(f"Unknown store '{value}'. Valid values: {valid}")


def visible_products_query() -> Select:
    """Return a base SELECT for products that are not hidden.

    Returns:
        SQLAlchemy Select statement filtered to non-hidden products.
    """
    return select(Product).where(Product.is_hidden.is_(False))


async def get_visible_products(session: AsyncSession) -> list[Product]:
    """Fetch all non-hidden products.

    Args:
        session: Active async database session.

    Returns:
        List of visible Product instances.
    """
    result = await session.execute(visible_products_query())
    return list(result.scalars().all())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_db_helpers.py -v`
Expected: PASS

- [ ] **Step 6: Update call sites — prices view**

Read `src/shopping_agent/routes/views/prices.py` and replace the inline `is_hidden == False` checks with calls to `get_visible_products()` or `visible_products_query()`.

- [ ] **Step 7: Update call sites — refresh route**

Read `src/shopping_agent/routes/api_prices/refresh.py` and replace inline visibility checks.

- [ ] **Step 8: Update call sites — prediction service**

Read `src/shopping_agent/services/prediction.py` and replace inline visibility checks.

- [ ] **Step 9: Replace Store enum conversions throughout**

Use `store_from_string()` from `db_helpers` everywhere `Store(store)` or `Store[store.upper()]` appears:
- `routes/api_orders.py`
- `routes/api_cart.py`
- `routes/api_shopping_list.py`
- `routes/api_prices/products.py`

- [ ] **Step 10: Run all tests**

Run: `pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 11: Commit**

```bash
git add src/shopping_agent/db_helpers.py tests/test_db_helpers.py \
  src/shopping_agent/routes/views/prices.py \
  src/shopping_agent/routes/api_prices/refresh.py \
  src/shopping_agent/services/prediction.py \
  src/shopping_agent/routes/api_orders.py \
  src/shopping_agent/routes/api_cart.py \
  src/shopping_agent/routes/api_shopping_list.py \
  src/shopping_agent/routes/api_prices/products.py
git commit -m "refactor: centralize store enum conversion and visibility filter in db_helpers"
```

---

### Task 4: Extract ProductMatch resolution to shared service

**Context:** The same pattern of "find the ProductMatch for a product, get the partner store's product" appears in `services/cart.py:25-34`, `routes/api_shopping_list.py:229-238`, and `routes/api_prices/refresh.py:87-94`. A bug fix would need to touch all three.

**Files:**
- Create: `src/shopping_agent/services/product_resolution.py`
- Modify: `src/shopping_agent/services/cart.py`
- Modify: `src/shopping_agent/routes/api_shopping_list.py`
- Modify: `src/shopping_agent/routes/api_prices/refresh.py`
- Test: `tests/test_product_resolution.py`

- [ ] **Step 1: Read all three duplication sites**

Read:
- `src/shopping_agent/services/cart.py` lines 14-50
- `src/shopping_agent/routes/api_shopping_list.py` lines 225-245
- `src/shopping_agent/routes/api_prices/refresh.py` lines 80-100

Note the exact query pattern used in each.

- [ ] **Step 2: Write the test**

```python
# tests/test_product_resolution.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from shopping_agent.services.product_resolution import get_partner_product


@pytest.mark.asyncio
async def test_get_partner_product_returns_none_when_no_match():
    """Returns None gracefully when no ProductMatch exists."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
    result = await get_partner_product(session, product_id=1, target_store="woolworths")
    assert result is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_product_resolution.py -v`
Expected: FAIL

- [ ] **Step 4: Create product_resolution.py**

Create `src/shopping_agent/services/product_resolution.py`:
```python
"""Utilities for resolving products across stores via ProductMatch."""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.product import Product, ProductMatch, Store


async def get_partner_product(
    session: AsyncSession,
    product_id: int,
    target_store: str,
) -> Product | None:
    """Find the matched product in the target store for a given product.

    Looks up the ProductMatch record where product_id is either the
    source or target, then returns the Product on the opposite side
    from target_store.

    Args:
        session: Active async database session.
        product_id: ID of the product to find a partner for.
        target_store: Store name to find the partner product in.

    Returns:
        The partner Product if a confirmed match exists, else None.
    """
    target = Store[target_store.upper()]

    stmt = (
        select(ProductMatch)
        .where(
            or_(
                ProductMatch.product1_id == product_id,
                ProductMatch.product2_id == product_id,
            )
        )
        .where(ProductMatch.is_confirmed.is_(True))
    )
    result = await session.execute(stmt)
    match = result.scalar_one_or_none()

    if match is None:
        return None

    partner_id = (
        match.product2_id
        if match.product1_id == product_id
        else match.product1_id
    )

    product_result = await session.execute(
        select(Product).where(Product.id == partner_id).where(Product.store == target)
    )
    return product_result.scalar_one_or_none()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_product_resolution.py -v`
Expected: PASS

- [ ] **Step 6: Update cart.py to use product_resolution**

Read `src/shopping_agent/services/cart.py`, find the duplicated resolution logic, replace with:
```python
from .product_resolution import get_partner_product
# ...
partner = await get_partner_product(session, product.id, target_store.value)
```

- [ ] **Step 7: Update api_shopping_list.py**

Replace the duplicated ProductMatch query block with a call to `get_partner_product`.

- [ ] **Step 8: Update api_prices/refresh.py**

Replace the duplicated ProductMatch query block with a call to `get_partner_product`.

- [ ] **Step 9: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass.

- [ ] **Step 10: Commit**

```bash
git add src/shopping_agent/services/product_resolution.py \
  tests/test_product_resolution.py \
  src/shopping_agent/services/cart.py \
  src/shopping_agent/routes/api_shopping_list.py \
  src/shopping_agent/routes/api_prices/refresh.py
git commit -m "refactor: extract ProductMatch resolution to shared service, eliminate 3x duplication"
```

---

## Chunk 3: Error Handling

### Task 5: Fix bare exception handlers

**Context:** `except Exception: pass` silently swallows errors in at least 3 locations, making production debugging nearly impossible.

**Files:**
- Modify: `src/shopping_agent/routes/api_orders.py`
- Modify: `src/shopping_agent/scrapers/coles.py`
- Modify: `src/shopping_agent/routes/api_prices/products.py`

- [ ] **Step 1: Read all three files**

Read:
- `src/shopping_agent/routes/api_orders.py` (full)
- `src/shopping_agent/scrapers/coles.py` lines 90-120
- `src/shopping_agent/routes/api_prices/products.py` (full)

Note every bare `except Exception`, `except Exception: pass`, or similar.

- [ ] **Step 2: Fix api_orders.py bare excepts**

Replace every `except Exception: pass` or `except Exception as e: pass` with:
```python
except Exception:
    logger.exception("Unexpected error in <describe context>")
    raise
```

Or if the error should be surfaced to the client as a failed SSE event:
```python
except Exception as exc:
    logger.exception("Order sync failed")
    yield f"data: {json.dumps({'error': str(exc)})}\n\n"
    return
```

Ensure `import logging; logger = logging.getLogger(__name__)` is at the top.

- [ ] **Step 3: Fix coles.py bare excepts**

Same pattern: log with `logger.exception(...)` and re-raise, or propagate the error as a meaningful exception type.

- [ ] **Step 4: Fix api_prices/products.py HTTP error handling**

Replace broad `except httpx.HTTPError` with specific handling:
```python
except httpx.HTTPStatusError as exc:
    if exc.response.status_code == 404:
        raise HTTPException(status_code=404, detail="Image not found")
    logger.warning("Image proxy failed with %d", exc.response.status_code)
    raise HTTPException(status_code=502, detail="Upstream error fetching image")
except httpx.RequestError:
    logger.exception("Network error fetching image")
    raise HTTPException(status_code=502, detail="Network error fetching image")
```

- [ ] **Step 5: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add src/shopping_agent/routes/api_orders.py \
  src/shopping_agent/scrapers/coles.py \
  src/shopping_agent/routes/api_prices/products.py
git commit -m "fix: replace bare exception handlers with logged errors and specific HTTP status handling"
```

---

### Task 6: Strengthen cookie validation in BaseScraper

**Context:** `scrapers/base.py` cookie import only checks JSON parse and list type — it doesn't validate that cookies have required fields (`name`, `value`, `domain`), so malformed cookies are silently stored and cause cryptic errors later.

**Files:**
- Modify: `src/shopping_agent/scrapers/base.py`
- Test: `tests/test_scraper_base.py`

- [ ] **Step 1: Read base.py cookie loading code**

Read `src/shopping_agent/scrapers/base.py` fully, focusing on cookie loading/validation.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_scraper_base.py
import pytest
from shopping_agent.scrapers.base import validate_cookie_list


def test_validate_cookie_list_rejects_missing_name():
    cookies = [{"value": "abc", "domain": "coles.com.au"}]
    with pytest.raises(ValueError, match="missing required field 'name'"):
        validate_cookie_list(cookies)


def test_validate_cookie_list_rejects_missing_value():
    cookies = [{"name": "session", "domain": "coles.com.au"}]
    with pytest.raises(ValueError, match="missing required field 'value'"):
        validate_cookie_list(cookies)


def test_validate_cookie_list_accepts_valid():
    cookies = [{"name": "session", "value": "abc123", "domain": "coles.com.au"}]
    # Should not raise
    validate_cookie_list(cookies)


def test_validate_cookie_list_rejects_non_list():
    with pytest.raises(ValueError, match="expected a list"):
        validate_cookie_list({"name": "session"})
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_scraper_base.py -v`
Expected: FAIL

- [ ] **Step 4: Add validate_cookie_list to base.py**

Add the validation function to `scrapers/base.py`:
```python
REQUIRED_COOKIE_FIELDS = {"name", "value", "domain"}


def validate_cookie_list(data: object) -> list[dict]:
    """Validate that data is a list of cookies with required fields.

    Args:
        data: Parsed JSON data to validate.

    Returns:
        The validated list of cookie dicts.

    Raises:
        ValueError: If data is not a list or any cookie is missing required fields.
    """
    if not isinstance(data, list):
        raise ValueError(f"expected a list of cookies, got {type(data).__name__}")
    for i, cookie in enumerate(data):
        for field in REQUIRED_COOKIE_FIELDS:
            if field not in cookie:
                raise ValueError(
                    f"cookie at index {i} missing required field '{field}'"
                )
    return data
```

Call `validate_cookie_list(parsed)` in the existing cookie loading function.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_scraper_base.py -v`
Expected: PASS

- [ ] **Step 6: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add src/shopping_agent/scrapers/base.py tests/test_scraper_base.py
git commit -m "fix: add cookie validation to BaseScraper to catch malformed cookies early"
```

---

## Chunk 4: Modularity — Split api_shopping_list.py

### Task 7: Audit and split api_shopping_list.py (491 lines)

**Context:** `routes/api_shopping_list.py` handles list CRUD, item add/remove, price lookups, store selection, and candidate generation. This is five concerns in one file, making it hard to locate logic and test.

**Files:**
- Create: `src/shopping_agent/routes/api_shopping_list/` (package)
  - `__init__.py`
  - `crud.py` — list create, get, delete
  - `items.py` — item add/update/remove
  - `stores.py` — store selection and price lookup
  - `candidates.py` — candidate generation
- Delete: `src/shopping_agent/routes/api_shopping_list.py` (after migration)
- Modify: `src/shopping_agent/main.py` — router import unchanged (same module path)

- [ ] **Step 1: Read api_shopping_list.py fully**

Read `src/shopping_agent/routes/api_shopping_list.py` in full. Identify every route handler and its responsibility.

Group them:
- `crud.py`: GET list, POST create, DELETE list
- `items.py`: POST add item, PATCH update item, DELETE item
- `stores.py`: POST select store, GET prices
- `candidates.py`: GET candidates, POST from predictions

- [ ] **Step 2: Create the package directory**

Create `src/shopping_agent/routes/api_shopping_list/__init__.py`:
```python
"""Shopping list route package.

Re-exports the combined router for backward-compatible imports.
"""
from .crud import router as crud_router
from .items import router as items_router
from .stores import router as stores_router
from .candidates import router as candidates_router
from fastapi import APIRouter

router = APIRouter()
router.include_router(crud_router)
router.include_router(items_router)
router.include_router(stores_router)
router.include_router(candidates_router)

__all__ = ["router"]
```

- [ ] **Step 3: Create crud.py**

Move list create/read/delete endpoints into `crud.py`. Each handler should be copied verbatim — no logic changes.

- [ ] **Step 4: Create items.py**

Move item add/update/remove handlers into `items.py`.

- [ ] **Step 5: Create stores.py**

Move store selection and price lookup handlers into `stores.py`.

- [ ] **Step 6: Create candidates.py**

Move candidate generation handlers into `candidates.py`.

- [ ] **Step 7: Delete original file**

Remove `src/shopping_agent/routes/api_shopping_list.py`.

- [ ] **Step 8: Verify app starts and routes are registered**

Run: `python -c "from shopping_agent.main import app; routes = [r.path for r in app.routes]; print(len(routes), 'routes registered')"`
Expected: Same number of routes as before.

- [ ] **Step 9: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass.

- [ ] **Step 10: Commit**

```bash
git add src/shopping_agent/routes/api_shopping_list/ \
  src/shopping_agent/main.py
git rm src/shopping_agent/routes/api_shopping_list.py
git commit -m "refactor: split 491-line api_shopping_list.py into focused sub-modules"
```

---

## Chunk 5: Type Hints and Docstrings

### Task 8: Add return type hints to service functions

**Context:** ~20 async service functions lack return type annotations, reducing IDE autocomplete quality and making refactoring risky.

**Files:**
- Modify: `src/shopping_agent/services/prediction.py`
- Modify: `src/shopping_agent/services/shopping_list.py`
- Modify: `src/shopping_agent/services/price_comparison.py`
- Modify: `src/shopping_agent/services/order_sync.py`
- Modify: `src/shopping_agent/services/cart.py`

- [ ] **Step 1: Read each service file**

Read all five service files in full.

- [ ] **Step 2: Add type hints to prediction.py**

For every function that lacks a return type annotation (`-> Type`), add one. Do not change logic. Examples:
```python
async def generate_predictions(session: AsyncSession) -> list[ConsumptionPrediction]:
async def _compute_interval(purchases: list[datetime]) -> float | None:
```

- [ ] **Step 3: Add type hints to shopping_list.py**

Same process. For dict return types that have stable keys, define a TypedDict:
```python
from typing import TypedDict

class ShoppingListSummary(TypedDict):
    list_id: int
    item_count: int
    total_price: float | None
```

- [ ] **Step 4: Add type hints to price_comparison.py**

Same process.

- [ ] **Step 5: Add type hints to order_sync.py and cart.py**

Same process.

- [ ] **Step 6: Run mypy**

Run: `mypy src/shopping_agent/services/ --ignore-missing-imports`
Expected: No new errors introduced (existing errors acceptable if pre-existing).

- [ ] **Step 7: Add docstrings to complex functions**

Add Google-style docstrings to any function in services/ that lacks one and has non-obvious logic. Minimum: `generate_candidates()`, `matches_to_comparisons()`, `resolve_display_names()`.

Format:
```python
def generate_candidates(...) -> ...:
    """Generate shopping list candidates from consumption predictions.

    Args:
        session: Active async database session.
        cutoff_days: Only include items predicted to run out within this many days.

    Returns:
        List of ShoppingListItem candidates ordered by urgency.
    """
```

- [ ] **Step 8: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass.

- [ ] **Step 9: Run ruff check**

Run: `ruff check src/shopping_agent/services/`
Expected: No errors (fix any introduced by edits).

- [ ] **Step 10: Commit**

```bash
git add src/shopping_agent/services/
git commit -m "refactor: add return type hints and docstrings to service functions"
```

---

## Chunk 6: HTML Generation in Routes

### Task 9: Move f-string HTML from routes to templates

**Context:** `routes/api_orders.py` builds an HTML table as an f-string (~lines 126-148). `routes/api_cart.py` injects inline JS as an HTML string. This breaks the template layer separation and prevents Jinja2 escaping.

**Files:**
- Create: `templates/fragments/_order_sync_row.html`
- Create: `templates/fragments/_cart_result.html`
- Modify: `src/shopping_agent/routes/api_orders.py`
- Modify: `src/shopping_agent/routes/api_cart.py`
- Modify: `src/shopping_agent/templating.py` — ensure Jinja2 env available in routes

- [ ] **Step 1: Read the f-string HTML in both routes**

Read `src/shopping_agent/routes/api_orders.py` lines 120-155 and `src/shopping_agent/routes/api_cart.py` lines 70-90.

- [ ] **Step 2: Identify what data is interpolated into the HTML**

Note every variable used inside the f-string. These become template context variables.

- [ ] **Step 3: Create _order_sync_row.html fragment**

Create `templates/fragments/_order_sync_row.html` with the table row HTML, using `{{ variable }}` syntax instead of `{variable}` f-string syntax. Jinja2 auto-escapes by default.

- [ ] **Step 4: Create _cart_result.html fragment**

Create `templates/fragments/_cart_result.html` with the cart result HTML/JS.

- [ ] **Step 5: Update api_orders.py to render template**

Replace the f-string HTML construction with:
```python
from ..templating import templates
# ...
html = templates.get_template("fragments/_order_sync_row.html").render(
    order=order, items=items
)
yield f"data: {json.dumps({'html': html})}\n\n"
```

- [ ] **Step 6: Update api_cart.py similarly**

- [ ] **Step 7: Run all tests**

Run: `pytest tests/ -v`
Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add templates/fragments/_order_sync_row.html \
  templates/fragments/_cart_result.html \
  src/shopping_agent/routes/api_orders.py \
  src/shopping_agent/routes/api_cart.py
git commit -m "refactor: move f-string HTML construction from routes into Jinja2 template fragments"
```

---

## Chunk 7: Template Accessibility

### Task 10: Fix accessibility issues in base.html and chart templates

**Context:** The hamburger menu button lacks `aria-label`, charts in canvas elements have no text fallback, and some color-only status indicators lack icons.

**Files:**
- Modify: `templates/base.html`
- Modify: `templates/prices.html` (or wherever charts are rendered)
- Modify: `templates/predictions.html`

- [ ] **Step 1: Read base.html fully**

Read `templates/base.html`.

- [ ] **Step 2: Add aria-label to hamburger button**

Find the mobile menu toggle button and add:
```html
<button aria-label="Open navigation menu" aria-expanded="false" ...>
```

Update the JavaScript toggle to also toggle `aria-expanded`.

- [ ] **Step 3: Add aria-label to chart canvases**

Read the chart templates. For each `<canvas>` tag, add:
```html
<canvas role="img" aria-label="Price history chart for {{ product.name }}">
  Price history data for {{ product.name }} — see table below for values.
</canvas>
```

- [ ] **Step 4: Commit**

```bash
git add templates/base.html templates/prices.html templates/predictions.html
git commit -m "fix: add ARIA labels to navigation button and chart elements for accessibility"
```

---

## Execution Order

1. **Chunk 1** (Security) — Do this first, non-negotiable.
2. **Chunk 2** (DRY) — Safe, mechanical refactors with tests.
3. **Chunk 3** (Error handling) — High value, low risk.
4. **Chunk 4** (Modularity) — More involved; ensure tests pass before and after.
5. **Chunk 5** (Type hints) — Low risk, high maintainability value.
6. **Chunk 6** (HTML in routes) — Medium risk; test rendered HTML manually.
7. **Chunk 7** (Accessibility) — Lowest risk, template-only changes.

> Each chunk can be stopped after and the codebase will be in a better state than before. Prioritize Chunks 1–3 if time-constrained.
