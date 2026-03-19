# Embedded MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a FastMCP streamable-HTTP server embedded in the FastAPI app, exposing 19 tools for grocery automation (predictions, shopping lists, cart, sync, price matching).

**Architecture:** One new file `routes/mcp.py` holds the FastMCP instance and all tool definitions. Three required service extractions enable clean reuse. The server mounts at `/mcp` via `app.mount()` in `main.py`.

**Tech Stack:** FastMCP ≥2.0 (streamable-HTTP transport), existing SQLAlchemy async sessions, existing scrapers and services — no new business logic.

**Spec:** `docs/superpowers/specs/` (see plan companion at `.claude/plans/cozy-humming-volcano.md`)

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `pyproject.toml` | Add `fastmcp>=2.0` dependency |
| Modify | `src/shopping_agent/services/shopping_list.py` | Add `assign_cheapest_stores()` and `add_item_to_list()` |
| Modify | `src/shopping_agent/routes/api_shopping_list/stores.py` | Delegate `submit_split` to new service |
| Modify | `src/shopping_agent/routes/api_shopping_list/items.py` | Delegate `add_product_to_list` to new service |
| Create | `src/shopping_agent/services/price_refresh.py` | Extracted awaitable price refresh logic |
| Modify | `src/shopping_agent/routes/api_prices/refresh.py` | Delegate `_do_price_refresh` to new service |
| Create | `src/shopping_agent/routes/mcp.py` | FastMCP instance + all 19 tool definitions |
| Modify | `src/shopping_agent/main.py` | Mount MCP server at `/mcp` |
| Modify | `tests/test_shopping_list.py` | Tests for new service functions |
| Create | `tests/test_price_refresh.py` | Tests for extracted price refresh service |

---

## Task 1: Add fastmcp dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dependency**

Edit `pyproject.toml` `dependencies` list to add:
```toml
"fastmcp>=2.0",
```

- [ ] **Step 2: Install**

```bash
pip install -e ".[dev]"
```

Expected: fastmcp installs without conflicts.

- [ ] **Step 3: Verify import works**

```bash
python -c "from fastmcp import FastMCP; print('FastMCP OK')"
```

Expected: `FastMCP OK`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add fastmcp dependency for embedded MCP server"
```

---

## Task 2: Extract `assign_cheapest_stores()` service function

**Files:**
- Modify: `src/shopping_agent/services/shopping_list.py` (add after `confirm_list`)
- Modify: `src/shopping_agent/routes/api_shopping_list/stores.py` (update `submit_split`)
- Test: `tests/test_shopping_list.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_shopping_list.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from shopping_agent.models import Store


class TestAssignCheapestStores:
    """Tests for assign_cheapest_stores() service function."""

    @pytest.mark.asyncio
    async def test_assigns_cheapest_store_per_item(self):
        """Items get assigned to whichever store is cheaper."""
        from shopping_agent.services.shopping_list import assign_cheapest_stores
        from shopping_agent.models import ShoppingListItem, ShoppingList, ListStatus

        # Build mock items
        item_coles_cheaper = MagicMock(spec=ShoppingListItem)
        item_coles_cheaper.coles_price = 1.50
        item_coles_cheaper.woolworths_price = 2.00
        item_coles_cheaper.chosen_store = Store.WOOLWORTHS
        item_coles_cheaper.is_removed = False

        item_ww_cheaper = MagicMock(spec=ShoppingListItem)
        item_ww_cheaper.coles_price = 3.00
        item_ww_cheaper.woolworths_price = 2.50
        item_ww_cheaper.chosen_store = Store.COLES
        item_ww_cheaper.is_removed = False

        shopping_list = MagicMock(spec=ShoppingList)
        shopping_list.id = 1
        shopping_list.status = ListStatus.DRAFT

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[
            # First call: get active list
            MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=shopping_list)))),
            # Second call: get items
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[item_coles_cheaper, item_ww_cheaper])))),
        ])
        session.commit = AsyncMock()

        result = await assign_cheapest_stores(session)

        assert result == 2  # two items assigned
        assert item_coles_cheaper.chosen_store == Store.COLES
        assert item_ww_cheaper.chosen_store == Store.WOOLWORTHS
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_active_list(self):
        """Returns 0 when no active shopping list exists."""
        from shopping_agent.services.shopping_list import assign_cheapest_stores

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        ))

        result = await assign_cheapest_stores(session)
        assert result == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_shopping_list.py::TestAssignCheapestStores -v
```

Expected: `ImportError` or `AttributeError` — `assign_cheapest_stores` not found.

- [ ] **Step 3: Implement `assign_cheapest_stores` in shopping_list.py**

Find the `confirm_list` function in `src/shopping_agent/services/shopping_list.py` and add after it:

```python
async def assign_cheapest_stores(session: AsyncSession) -> int:
    """Assign each active list item to its cheapest available store.

    Args:
        session: Async database session.

    Returns:
        Number of items whose store was assigned (0 if no active list).
    """
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if not shopping_list:
        return 0

    items_result = await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == shopping_list.id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )
    items = items_result.scalars().all()
    for item in items:
        item.chosen_store = choose_best_store(
            item.coles_price, item.woolworths_price, item.chosen_store or Store.COLES
        )
    await session.commit()
    return len(items)
```

- [ ] **Step 4: Update `submit_split` to delegate to service**

In `src/shopping_agent/routes/api_shopping_list/stores.py`, add import and update `submit_split`:

```python
# Add to imports at top:
from ...services.shopping_list import (
    assign_cheapest_stores,
    choose_best_store,
    get_shopping_list_context as _shopping_list_context,
)

# Replace submit_split body:
@router.post("/submit-split")
async def submit_split(session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    """Set each item to its cheapest available store, confirm, and redirect to review."""
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if not shopping_list:
        return RedirectResponse("/shopping-list", status_code=303)
    await assign_cheapest_stores(session)
    shopping_list.status = ListStatus.CONFIRMED
    await session.commit()
    return RedirectResponse("/confirm", status_code=303)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_shopping_list.py::TestAssignCheapestStores -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/shopping_agent/services/shopping_list.py src/shopping_agent/routes/api_shopping_list/stores.py tests/test_shopping_list.py
git commit -m "refactor: extract assign_cheapest_stores() service function"
```

---

## Task 3: Extract `add_item_to_list()` service function

**Files:**
- Modify: `src/shopping_agent/services/shopping_list.py` (add function)
- Modify: `src/shopping_agent/routes/api_shopping_list/items.py` (delegate to service)
- Test: `tests/test_shopping_list.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shopping_list.py`:

```python
class TestAddItemToList:
    """Tests for add_item_to_list() service function."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_active_list(self):
        """Returns None if no active shopping list exists."""
        from shopping_agent.services.shopping_list import add_item_to_list

        session = AsyncMock()
        # First execute: get active list
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        ))

        result = await add_item_to_list(session, product_id=1)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_product_not_found(self):
        """Returns None if product_id doesn't exist."""
        from shopping_agent.services.shopping_list import add_item_to_list
        from shopping_agent.models import ShoppingList, ListStatus

        shopping_list = MagicMock(spec=ShoppingList)
        shopping_list.id = 1
        shopping_list.status = ListStatus.DRAFT

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=shopping_list)))
        ))
        session.get = AsyncMock(return_value=None)  # product not found

        result = await add_item_to_list(session, product_id=999)
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_shopping_list.py::TestAddItemToList -v
```

Expected: `ImportError` — `add_item_to_list` not found.

- [ ] **Step 3: Implement `add_item_to_list` in shopping_list.py**

Add after `assign_cheapest_stores` in `src/shopping_agent/services/shopping_list.py`. Also add to the imports at top: `from sqlalchemy.exc import IntegrityError` and ensure `Product`, `ProductMatch` are imported.

```python
async def add_item_to_list(
    session: AsyncSession,
    product_id: int,
    quantity: int = 1,
) -> ShoppingListItem | None:
    """Add a product to the active shopping list.

    Handles partner resolution (finds cross-store price), deduplication
    (increments quantity if already on list), and IntegrityError (concurrent
    insert race condition).

    Args:
        session: Async database session.
        product_id: ID of the product to add.
        quantity: Quantity to add (default 1). If product already on list,
            increments by this amount.

    Returns:
        The ShoppingListItem added or updated, or None if no active list
        or product not found.
    """
    from sqlalchemy.exc import IntegrityError

    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if not shopping_list:
        return None

    product = await session.get(Product, product_id)
    if not product:
        return None

    from .product_resolution import get_partner_product

    # Determine partner store and pre-load partner for price resolution
    partner_store = "woolworths" if product.store == Store.COLES else "coles"
    partner_product = await get_partner_product(session, product_id, partner_store)
    partner_id = partner_product.id if partner_product else None

    # Already on list? (check both product and matched partner)
    candidate_ids = [product_id] + ([partner_id] if partner_id else [])
    existing = (await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == shopping_list.id,
            ShoppingListItem.product_id.in_(candidate_ids),
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )).scalars().first()

    if existing:
        existing.quantity += quantity
        await session.commit()
        return existing

    # Resolve prices
    coles_price = None
    woolworths_price = None
    chosen_store = product.store

    if partner_product:
        coles_p = product if product.store == Store.COLES else partner_product
        ww_p = product if product.store == Store.WOOLWORTHS else partner_product
        coles_price = coles_p.current_price if coles_p else None
        woolworths_price = ww_p.current_price if ww_p else None
        chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
    else:
        if product.store == Store.COLES:
            coles_price = product.current_price
        else:
            woolworths_price = product.current_price

    try:
        item = ShoppingListItem(
            shopping_list_id=shopping_list.id,
            product_id=product_id,
            quantity=quantity,
            coles_price=coles_price,
            woolworths_price=woolworths_price,
            chosen_store=chosen_store,
            is_user_added=True,
        )
        session.add(item)
        await session.commit()
        return item
    except IntegrityError:
        # Concurrent insert race — rollback and increment existing
        await session.rollback()
        existing = (await session.execute(
            select(ShoppingListItem).where(
                ShoppingListItem.shopping_list_id == shopping_list.id,
                ShoppingListItem.product_id.in_(candidate_ids),
                ShoppingListItem.is_removed == False,  # noqa: E712
            )
        )).scalars().first()
        if existing:
            existing.quantity += quantity
            await session.commit()
        return existing
```

- [ ] **Step 4: Update route handler to delegate to service**

In `src/shopping_agent/routes/api_shopping_list/items.py`, update imports and replace `add_product_to_list` body:

```python
# Add to imports:
from ...services.shopping_list import (
    add_item_to_list as _add_item_to_list,
    choose_best_store,
    get_shopping_list_context as _shopping_list_context,
    remove_item,
    update_item_quantity,
    update_item_store,
)

# Replace add_product_to_list:
@router.post("/items/add-product")
async def add_product_to_list(
    product_id: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Add a product (by id) to the active shopping list."""
    item = await _add_item_to_list(session, product_id=product_id, quantity=1)
    if item is None:
        return HTMLResponse('<span class="text-red-600 text-xs">No active list or product not found.</span>')
    status = "Added ✓" if item.quantity == 1 else "Qty updated ✓"
    ctx = await _shopping_list_context(session)
    list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(
        f'<span class="text-green-600 text-xs">{status}</span>'
        f'<div id="list-content" hx-swap-oob="innerHTML">{list_html}</div>'
    )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_shopping_list.py::TestAddItemToList -v
```

Expected: PASS

- [ ] **Step 6: Run all tests to check nothing is broken**

```bash
pytest -v
```

Expected: all green

- [ ] **Step 7: Commit**

```bash
git add src/shopping_agent/services/shopping_list.py src/shopping_agent/routes/api_shopping_list/items.py tests/test_shopping_list.py
git commit -m "refactor: extract add_item_to_list() service function"
```

---

## Task 4: Extract price refresh logic to service

**Files:**
- Create: `src/shopping_agent/services/price_refresh.py`
- Modify: `src/shopping_agent/routes/api_prices/refresh.py` (delegate to service)
- Create: `tests/test_price_refresh.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_price_refresh.py`:

```python
"""Tests for the price_refresh service."""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


class TestPriceRefreshService:
    """Tests for do_price_refresh() service function."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_products(self):
        """Returns (0, 0) tuple when store has no products."""
        from shopping_agent.services.price_refresh import do_price_refresh
        from shopping_agent.models import Store

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session_ctx)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        session_ctx.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )

        with patch("shopping_agent.services.price_refresh.async_session", return_value=session_ctx):
            updated, total = await do_price_refresh(Store.COLES)

        assert updated == 0
        assert total == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_price_refresh.py -v
```

Expected: `ModuleNotFoundError` — `price_refresh` module not found.

- [ ] **Step 3: Create `services/price_refresh.py`**

Create `src/shopping_agent/services/price_refresh.py` by extracting the core logic from `_do_price_refresh` in `routes/api_prices/refresh.py`. The key change is: no `_refresh_progress` mutations — just return `(updated: int, total: int)`.

```python
"""Price refresh service — fetch current prices for all products of a store."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from ..config import COLES_PRICE_REFRESH_CONCURRENCY, WOOLWORTHS_PRICE_REFRESH_CONCURRENCY
from ..database import async_session
from ..db_helpers import visible_products_query
from ..models import ListStatus, PriceHistory, Product, ProductMatch, ShoppingList, ShoppingListItem, Store
from ..scrapers.coles import coles_scraper as _coles_scraper
from ..scrapers.woolworths import woolworths_scraper as _ww_scraper

logger = logging.getLogger(__name__)


async def do_price_refresh(store_enum: Store) -> tuple[int, int]:
    """Refresh current prices for all visible products of a given store.

    Fetches each product's current price concurrently (respecting per-store
    concurrency limits), updates Product.current_price, upserts today's
    PriceHistory entry, and syncs prices on active ShoppingListItems.

    Args:
        store_enum: The store to refresh prices for.

    Returns:
        Tuple of (updated_count, total_count) — number of products whose price
        was successfully fetched and total products processed.
    """
    scraper = _coles_scraper if store_enum == Store.COLES else _ww_scraper
    concurrency = COLES_PRICE_REFRESH_CONCURRENCY if store_enum == Store.COLES else WOOLWORTHS_PRICE_REFRESH_CONCURRENCY

    async with async_session() as session:
        result = await session.execute(
            visible_products_query().where(Product.store == store_enum)
        )
        products = list(result.scalars().all())
        product_ids = [p.id for p in products]
        product_map = {p.id: p for p in products}

        now_utc = datetime.now(timezone.utc)
        today_start_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start_utc = today_start_utc + timedelta(days=1)
        ph_rows = await session.execute(
            select(PriceHistory).where(
                PriceHistory.product_id.in_(product_ids),
                PriceHistory.recorded_at >= today_start_utc,
                PriceHistory.recorded_at < tomorrow_start_utc,
            )
        )
        today_ph_ids: dict[int, int] = {ph.product_id: ph.id for ph in ph_rows.scalars()}

        match_rows = await session.execute(
            select(ProductMatch)
            .options(selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b))
            .where(
                or_(
                    ProductMatch.product_a_id.in_(product_ids),
                    ProductMatch.product_b_id.in_(product_ids),
                ),
                ProductMatch.is_rejected == False,  # noqa: E712
            )
        )
        partner_map: dict[int, Product] = {}
        for m in match_rows.scalars():
            if m.product_a_id in product_map:
                partner_map[m.product_a_id] = m.product_b
            if m.product_b_id in product_map:
                partner_map[m.product_b_id] = m.product_a

        all_affected_ids = set(product_ids) | {p.id for p in partner_map.values()}
        sli_rows = await session.execute(
            select(ShoppingListItem)
            .join(ShoppingList, ShoppingListItem.shopping_list_id == ShoppingList.id)
            .where(
                ShoppingList.status != ListStatus.ORDERED,
                ShoppingListItem.is_removed == False,  # noqa: E712
                ShoppingListItem.product_id.in_(all_affected_ids),
            )
        )
        sli_ids_by_product: dict[int, list[int]] = {}
        for sli in sli_rows.scalars():
            sli_ids_by_product.setdefault(sli.product_id, []).append(sli.id)

    if not products:
        return 0, 0

    logger.info("[PriceRefresh] Starting %s refresh for %d products", store_enum.value, len(products))
    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(product: Product) -> bool:
        async with sem:
            try:
                try:
                    scraped = await asyncio.wait_for(
                        scraper.get_product_price(product.store_product_id, product.name),
                        timeout=20.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("[PriceRefresh] Timeout fetching %s", product.store_product_id)
                    scraped = None
                async with async_session() as session:
                    db_product = await session.get(Product, product.id)
                    if db_product:
                        if scraped and scraped.current_price and scraped.is_available:
                            db_product.current_price = scraped.current_price
                            db_product.is_available = True
                            if scraped.unit_price:
                                db_product.unit_price = scraped.unit_price
                            if scraped.unit_price_measure:
                                db_product.unit_price_measure = scraped.unit_price_measure
                            if scraped.image_url:
                                db_product.image_url = scraped.image_url

                            ph_id = today_ph_ids.get(product.id)
                            if ph_id:
                                existing_ph = await session.get(PriceHistory, ph_id)
                                if existing_ph:
                                    existing_ph.price = scraped.current_price
                            else:
                                session.add(PriceHistory(
                                    product_id=product.id, store=store_enum, price=scraped.current_price
                                ))

                            affected_ids = [product.id]
                            partner = partner_map.get(product.id)
                            if partner:
                                affected_ids.append(partner.id)
                            for pid in affected_ids:
                                for sli_id in sli_ids_by_product.get(pid, []):
                                    sli = await session.get(ShoppingListItem, sli_id)
                                    if sli:
                                        if store_enum == Store.COLES:
                                            sli.coles_price = scraped.current_price
                                        else:
                                            sli.woolworths_price = scraped.current_price

                        elif scraped is not None and not scraped.is_available:
                            db_product.is_available = False
                            db_product.current_price = None
                            affected_ids = [product.id]
                            partner = partner_map.get(product.id)
                            if partner:
                                affected_ids.append(partner.id)
                            for pid in affected_ids:
                                for sli_id in sli_ids_by_product.get(pid, []):
                                    sli = await session.get(ShoppingListItem, sli_id)
                                    if sli:
                                        if store_enum == Store.COLES:
                                            sli.coles_price = None
                                        else:
                                            sli.woolworths_price = None
                        await session.commit()
                return bool(scraped and scraped.current_price)
            except Exception as e:
                logger.error("[PriceRefresh] Error for product %s: %s", product.store_product_id, e)
            return False

    results = await asyncio.gather(*[fetch_one(p) for p in products])
    updated = sum(results)
    logger.info("[PriceRefresh] %s done: %d/%d updated", store_enum.value, updated, len(products))
    return updated, len(products)
```

- [ ] **Step 4: Update route handler to call service**

In `src/shopping_agent/routes/api_prices/refresh.py`, replace the call to `background_tasks.add_task(_do_price_refresh, store_enum)` with a wrapper that calls the service and updates `_refresh_progress`:

```python
# Add import at top:
from ...services.price_refresh import do_price_refresh

# Replace _do_price_refresh body:
async def _do_price_refresh(store_enum: Store) -> None:
    """Background task wrapper: delegates to service and updates progress."""
    key = store_enum.value
    _refresh_progress[key] = {"done": 0, "total": 0, "running": True}
    updated = 0
    total = 0
    try:
        updated, total = await do_price_refresh(store_enum)
    except Exception:
        logger.exception("[PriceRefresh] Unexpected error during %s refresh", store_enum.value)
    finally:
        _refresh_progress[key] = {"done": total, "total": total, "running": False, "updated": updated}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_price_refresh.py -v
pytest -v
```

Expected: all green

- [ ] **Step 6: Commit**

```bash
git add src/shopping_agent/services/price_refresh.py src/shopping_agent/routes/api_prices/refresh.py tests/test_price_refresh.py
git commit -m "refactor: extract do_price_refresh() to services/price_refresh.py"
```

---

## Task 5: Create MCP server — read-only tools

**Files:**
- Create: `src/shopping_agent/routes/mcp.py`

Tools: `get_auth_status`, `get_predictions`, `get_shopping_list`, `get_shopping_list_history`, `search_products`, `get_price_comparison`

- [ ] **Step 1: Create `src/shopping_agent/routes/mcp.py` with read-only tools**

```python
"""Embedded MCP server for the shopping agent.

Exposes 19 tools for LLM agents to interact with grocery automation:
predictions, shopping lists, cart, order sync, price refresh, and product matching.

Mount: app.mount("/mcp", mcp.http_app()) in main.py
"""
import logging
from dataclasses import asdict

from fastmcp import FastMCP

from ..database import async_session
from ..db_helpers import store_from_string
from ..models import ListStatus, Product, ProductMatch, ShoppingList, Store
from ..services.prediction import get_predictions_with_match_info
from ..services.prediction import refresh_predictions as _refresh_predictions
from ..services.shopping_list import (
    add_item_to_list,
    assign_cheapest_stores,
    confirm_list,
    generate_shopping_list,
    get_active_list,
    get_list_history,
    get_shopping_list_context,
    remove_item,
    update_item_quantity,
)
from ..services.cart import add_to_cart
from ..services.price_comparison import (
    compare_product_prices,
    find_or_create_match,
    match_unmatched_products,
)
from ..services.price_refresh import do_price_refresh
from ..services.order_sync import sync_orders as _sync_orders
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper

logger = logging.getLogger(__name__)

mcp = FastMCP("shopping-agent")


def _scraper_for(store: str):
    """Return the scraper instance for the given store name."""
    s = store_from_string(store)
    return coles_scraper if s == Store.COLES else woolworths_scraper


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_auth_status(store: str) -> dict:
    """Check whether valid session cookies are stored for a store.

    Args:
        store: Store name — "coles" or "woolworths".

    Returns:
        {"store": str, "authenticated": bool, "message": str}
    """
    try:
        scraper = _scraper_for(store)
        authenticated = await scraper.is_authenticated()
        return {
            "store": store,
            "authenticated": authenticated,
            "message": "Connected" if authenticated else f"Not authenticated — import cookies for {store} first",
        }
    except ValueError as e:
        return {"store": store, "authenticated": False, "message": str(e)}


@mcp.tool()
async def get_predictions() -> list[dict]:
    """Get consumption predictions — what products are running low and when.

    Returns a list of predictions ordered by predicted runout date, with
    product name, store, confidence score, days until runout, and whether
    a cross-store price match exists.
    """
    async with async_session() as session:
        predictions = await get_predictions_with_match_info(session)
    return [
        {
            "product_id": p.product_id,
            "product_name": p.product.name,
            "store": p.product.store.value,
            "predicted_runout_date": str(p.predicted_runout_date) if p.predicted_runout_date else None,
            "days_until_runout": p.days_until_runout,
            "confidence_score": round(p.confidence_score, 2),
            "last_purchased_date": str(p.last_purchased_date) if p.last_purchased_date else None,
            "last_purchase_store": p.last_purchase_store.value if p.last_purchase_store else None,
            "is_matched": p.is_matched,
            "matched_product_name": p.matched_product.name if p.matched_product else None,
        }
        for p in predictions
    ]


@mcp.tool()
async def get_shopping_list() -> dict:
    """Get the current active shopping list with items, prices, and store assignments.

    Returns the active DRAFT or CONFIRMED list with per-item details.
    If no active list exists, returns {"shopping_list": null}.
    """
    async with async_session() as session:
        shopping_list = await get_active_list(session)
        if not shopping_list:
            return {"shopping_list": None, "items": []}
        items = [
            {
                "item_id": item.id,
                "product_id": item.product_id,
                "quantity": item.quantity,
                "coles_price": item.coles_price,
                "woolworths_price": item.woolworths_price,
                "chosen_store": item.chosen_store.value if item.chosen_store else None,
                "is_user_added": item.is_user_added,
            }
            for item in shopping_list.items
            if not item.is_removed
        ]
    return {
        "list_id": shopping_list.id,
        "name": shopping_list.name,
        "status": shopping_list.status.value,
        "item_count": len(items),
        "items": items,
    }


@mcp.tool()
async def get_shopping_list_history() -> list[dict]:
    """Get past ORDERED shopping lists with summaries.

    Returns a list of completed lists ordered by most recent first.
    """
    async with async_session() as session:
        history = await get_list_history(session)
    return [
        {
            "list_id": row["id"],
            "name": row["name"],
            "created_at": str(row["created_at"]),
            "status": row["status"].value,
            "store": row["store"].value if row["store"] else None,
            "item_count": row["item_count"],
            "total": row["total"],
        }
        for row in history
    ]


@mcp.tool()
async def search_products(query: str, store: str | None = None) -> list[dict]:
    """Search the Coles and/or Woolworths product catalog.

    Args:
        query: Product search query (e.g. "full cream milk 2L").
        store: Optional store filter — "coles" or "woolworths". Searches both if omitted.

    Returns:
        List of matching products with name, price, store, store_product_id.
        Requires valid cookies for the target store(s) — returns error entry if not authenticated.
    """
    results = []
    stores_to_search = []
    if store:
        try:
            stores_to_search = [store_from_string(store)]
        except ValueError as e:
            return [{"error": str(e)}]
    else:
        stores_to_search = [Store.COLES, Store.WOOLWORTHS]

    for s in stores_to_search:
        scraper = coles_scraper if s == Store.COLES else woolworths_scraper
        if not await scraper.is_authenticated():
            results.append({"store": s.value, "error": f"Not authenticated for {s.value}"})
            continue
        try:
            products = await scraper.search_product(query)
            for p in products:
                results.append({
                    "store": s.value,
                    "store_product_id": p.store_product_id,
                    "name": p.name,
                    "brand": p.brand,
                    "current_price": p.current_price,
                    "unit_size": p.unit_size,
                    "is_available": p.is_available,
                })
        except Exception as e:
            results.append({"store": s.value, "error": str(e)})

    return results


@mcp.tool()
async def get_price_comparison(product_id: int) -> dict:
    """Get Coles vs Woolworths price comparison for a product.

    Looks up the ProductMatch for the given product and returns both prices,
    the cheaper store, and the potential savings.

    Args:
        product_id: Database ID of the product.

    Returns:
        Price comparison with coles_price, woolworths_price, cheaper_store, savings.
        Returns error if product not found or no match exists.
    """
    async with async_session() as session:
        comparisons = await compare_product_prices(session, [product_id])
    if not comparisons:
        return {"error": f"No price comparison found for product {product_id}"}
    c = comparisons[0]
    return {
        "product_name": c.product_name,
        "unit_size": c.unit_size,
        "coles_price": c.coles_price,
        "woolworths_price": c.woolworths_price,
        "cheaper_store": c.cheaper_store.value if c.cheaper_store else None,
        "savings": c.savings,
        "match_confidence": c.match_confidence,
        "is_confirmed": c.is_confirmed,
    }
```

- [ ] **Step 2: Verify the file imports correctly**

```bash
python -c "from shopping_agent.routes.mcp import mcp; print('MCP OK')"
```

Expected: `MCP OK`

- [ ] **Step 3: Commit**

```bash
git add src/shopping_agent/routes/mcp.py
git commit -m "feat: add MCP server with read-only tools (auth, predictions, shopping list, prices)"
```

---

## Task 6: Add shopping list management tools to MCP

**Files:**
- Modify: `src/shopping_agent/routes/mcp.py` (append tools)

Tools: `create_shopping_list`, `add_item_to_list`, `update_item_quantity`, `remove_item_from_list`, `assign_cheapest_store`, `confirm_shopping_list`

- [ ] **Step 1: Add shopping list management tools**

Append to `src/shopping_agent/routes/mcp.py`:

```python
# ---------------------------------------------------------------------------
# Shopping list management tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_shopping_list(from_predictions: bool = False) -> dict:
    """Create a new DRAFT shopping list.

    Args:
        from_predictions: If True, pre-populate the list from consumption
            predictions (products predicted to run out within the lookahead window).
            If False, create an empty list.

    Returns:
        {"list_id": int, "item_count": int, "status": str}
    """
    async with async_session() as session:
        if from_predictions:
            shopping_list = await generate_shopping_list(session)
        else:
            shopping_list = ShoppingList(name="Shopping List", status=ListStatus.DRAFT)
            session.add(shopping_list)
            await session.commit()
        item_count = sum(1 for item in shopping_list.items if not getattr(item, "is_removed", False))
    return {"list_id": shopping_list.id, "item_count": item_count, "status": shopping_list.status.value}


@mcp.tool()
async def add_item_to_shopping_list(product_id: int, quantity: int = 1) -> dict:
    """Add a product to the active shopping list.

    If the product (or its cross-store match) is already on the list,
    the quantity is incremented instead of adding a duplicate.

    Args:
        product_id: Database ID of the product to add.
        quantity: Quantity to add (default 1).

    Returns:
        {"item_id": int, "product_id": int, "quantity": int, "status": str}
        or {"error": str} if no active list or product not found.
    """
    async with async_session() as session:
        item = await add_item_to_list(session, product_id=product_id, quantity=quantity)
    if item is None:
        return {"error": "No active shopping list or product not found"}
    return {
        "item_id": item.id,
        "product_id": item.product_id,
        "quantity": item.quantity,
        "chosen_store": item.chosen_store.value if item.chosen_store else None,
        "status": "added",
    }


@mcp.tool()
async def update_list_item_quantity(item_id: int, quantity: int) -> dict:
    """Update the quantity of an item on the active shopping list.

    Args:
        item_id: Database ID of the ShoppingListItem.
        quantity: New quantity (must be >= 1).

    Returns:
        {"item_id": int, "quantity": int} or {"error": str}
    """
    if quantity < 1:
        return {"error": "Quantity must be at least 1"}
    async with async_session() as session:
        await update_item_quantity(session, item_id, quantity)
    return {"item_id": item_id, "quantity": quantity}


@mcp.tool()
async def remove_list_item(item_id: int) -> dict:
    """Remove an item from the active shopping list (soft-delete).

    Args:
        item_id: Database ID of the ShoppingListItem to remove.

    Returns:
        {"item_id": int, "removed": bool}
    """
    async with async_session() as session:
        await remove_item(session, item_id)
    return {"item_id": item_id, "removed": True}


@mcp.tool()
async def assign_cheapest_store_to_all() -> dict:
    """Assign each item on the active list to its cheapest available store.

    Uses current Coles and Woolworths prices to pick the cheaper option
    per item. Does NOT confirm the list — call confirm_shopping_list() after
    to proceed to cart.

    Returns:
        {"items_assigned": int} or {"error": str} if no active list.
    """
    async with async_session() as session:
        count = await assign_cheapest_stores(session)
    if count == 0:
        return {"error": "No active list or no items to assign"}
    return {"items_assigned": count}


@mcp.tool()
async def confirm_shopping_list() -> dict:
    """Confirm the active shopping list, making it ready for cart addition.

    The list must be in DRAFT status. After confirming, use
    add_confirmed_list_to_cart() to add items to a store's cart.

    Returns:
        {"list_id": int, "status": "confirmed"} or {"error": str}
    """
    async with async_session() as session:
        shopping_list = await get_active_list(session)
        if not shopping_list:
            return {"error": "No active shopping list found"}
        confirmed = await confirm_list(session, shopping_list.id)
    return {"list_id": confirmed.id, "status": confirmed.status.value}
```

- [ ] **Step 2: Verify import still works**

```bash
python -c "from shopping_agent.routes.mcp import mcp; print('MCP OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/shopping_agent/routes/mcp.py
git commit -m "feat: add shopping list management MCP tools"
```

---

## Task 7: Add cart, sync, refresh, and prediction tools

**Files:**
- Modify: `src/shopping_agent/routes/mcp.py` (append tools)

Tools: `add_confirmed_list_to_cart`, `sync_orders`, `refresh_prices`, `refresh_predictions`

- [ ] **Step 1: Add cart and sync tools**

Append to `src/shopping_agent/routes/mcp.py`:

```python
# ---------------------------------------------------------------------------
# Cart tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def add_confirmed_list_to_cart(store: str) -> dict:
    """Add all confirmed shopping list items for a store to its cart.

    The list must be CONFIRMED (call confirm_shopping_list() first).
    Items assigned to the specified store are resolved to store product IDs
    and added via the store's API.

    ⚠️  This action adds items to your real grocery cart.

    Args:
        store: Target store — "coles" or "woolworths".

    Returns:
        {"success": bool, "count": int, "cart_url": str, "message": str,
         "failed_item_ids": list[int]} or {"error": str}
    """
    try:
        store_enum = store_from_string(store)
    except ValueError as e:
        return {"error": str(e)}

    scraper = coles_scraper if store_enum == Store.COLES else woolworths_scraper
    if not await scraper.is_authenticated():
        return {"error": f"Not authenticated for {store} — import cookies first"}

    async with async_session() as session:
        result = await add_to_cart(session, store_enum)
    return dict(result)


# ---------------------------------------------------------------------------
# Data sync & refresh tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def sync_orders(store: str, limit: int | None = None) -> dict:
    """Fetch order history from a store and sync it to the local database.

    Streams orders from the store's API, upserts them, and creates product
    records for any new items discovered.

    ⚠️  This can be slow for large order histories (1-2 minutes).
    Use the `limit` parameter to cap the number of orders fetched.

    Args:
        store: Store to sync — "coles" or "woolworths".
        limit: Maximum number of orders to fetch (default: all).

    Returns:
        {"store": str, "new_orders": int} or {"error": str}
    """
    try:
        store_enum = store_from_string(store)
    except ValueError as e:
        return {"error": str(e)}

    scraper = coles_scraper if store_enum == Store.COLES else woolworths_scraper
    if not await scraper.is_authenticated():
        return {"error": f"Not authenticated for {store} — import cookies first"}

    fetch_limit = limit or 200  # reasonable default cap
    scraped_orders = []
    async for order in scraper.stream_order_history(limit=fetch_limit):
        scraped_orders.append(order)

    async with async_session() as session:
        new_count = await _sync_orders(session, scraped_orders, store_enum)

    return {"store": store, "new_orders": new_count, "orders_fetched": len(scraped_orders)}


@mcp.tool()
async def refresh_prices(store: str) -> dict:
    """Refresh current prices for all products in a store.

    Fetches the latest price for every known product and updates the database.
    Also updates any active shopping list items with fresh prices.

    ⚠️  This can be slow (minutes for large product catalogs, especially
    Woolworths which has rate limiting). Requires valid store cookies.

    Args:
        store: Store to refresh — "coles" or "woolworths".

    Returns:
        {"store": str, "updated": int, "total": int} or {"error": str}
    """
    try:
        store_enum = store_from_string(store)
    except ValueError as e:
        return {"error": str(e)}

    scraper = coles_scraper if store_enum == Store.COLES else woolworths_scraper
    if not await scraper.is_authenticated():
        return {"error": f"Not authenticated for {store} — import cookies first"}

    updated, total = await do_price_refresh(store_enum)
    return {"store": store, "updated": updated, "total": total}


@mcp.tool()
async def refresh_predictions() -> dict:
    """Recompute all consumption predictions from order history.

    Analyzes purchase intervals and quantities for each product and updates
    the predicted runout dates and confidence scores.

    Returns:
        {"predictions_updated": int}
    """
    async with async_session() as session:
        count = await _refresh_predictions(session)
    return {"predictions_updated": count}
```

- [ ] **Step 2: Verify import still works**

```bash
python -c "from shopping_agent.routes.mcp import mcp; print('MCP OK')"
```

- [ ] **Step 4: Commit**

```bash
git add src/shopping_agent/routes/mcp.py
git commit -m "feat: add cart, sync, and refresh MCP tools"
```

---

## Task 8: Add product matching tools

**Files:**
- Modify: `src/shopping_agent/routes/mcp.py` (append tools)

Tools: `match_products`, `find_product_match`, `confirm_product_match`

- [ ] **Step 1: Add product matching tools**

Append to `src/shopping_agent/routes/mcp.py`:

```python
# ---------------------------------------------------------------------------
# Product matching tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def match_products(store: str | None = None) -> dict:
    """Auto-match unmatched products using fuzzy name matching.

    Finds unmatched products and attempts to pair them with their cross-store
    equivalent using rapidfuzz name similarity.

    Args:
        store: Store whose unmatched products to process — "coles" or
            "woolworths". If omitted, processes both stores.

    Returns:
        {"matches_created": int} or per-store breakdown if both processed.
    """
    stores_to_process = []
    if store:
        try:
            stores_to_process = [store_from_string(store)]
        except ValueError as e:
            return {"error": str(e)}
    else:
        stores_to_process = [Store.COLES, Store.WOOLWORTHS]

    results: dict[str, int] = {}
    for s in stores_to_process:
        async with async_session() as session:
            count = await match_unmatched_products(session, s)
        results[s.value] = count

    total = sum(results.values())
    return {"matches_created": total, "by_store": results}


@mcp.tool()
async def find_product_match(product_id: int, query: str | None = None) -> dict:
    """Search for a cross-store match for a product.

    Looks for an existing match first, then falls back to local fuzzy matching.
    If a query is provided, also searches the opposite store's catalog.

    Args:
        product_id: Database ID of the product to find a match for.
        query: Optional search query to use for catalog search (uses product
            name if omitted).

    Returns:
        Match details if found, or {"match": null} if no match could be found.
    """
    async with async_session() as session:
        product = await session.get(Product, product_id)
        if not product:
            return {"error": f"Product {product_id} not found"}

        target_store = Store.WOOLWORTHS if product.store == Store.COLES else Store.COLES
        target_scraper = woolworths_scraper if target_store == Store.WOOLWORTHS else coles_scraper

        # Use scraper only if authenticated and query provided
        scraper_to_use = None
        if query and await target_scraper.is_authenticated():
            scraper_to_use = target_scraper

        match = await find_or_create_match(session, product, target_store, scraper=scraper_to_use)
        if not match:
            return {"match": None, "message": "No match found"}

        partner_id = match.product_b_id if match.product_a_id == product_id else match.product_a_id
        partner = await session.get(Product, partner_id)

    return {
        "match_id": match.id,
        "product_id": product_id,
        "partner_product_id": partner_id,
        "partner_name": partner.name if partner else None,
        "partner_store": partner.store.value if partner else None,
        "confidence": match.confidence,
        "match_method": match.match_method,
        "is_confirmed": match.is_confirmed,
        "is_rejected": match.is_rejected,
    }


@mcp.tool()
async def confirm_product_match(match_id: int) -> dict:
    """Mark a ProductMatch as confirmed (human-verified correct).

    Args:
        match_id: Database ID of the ProductMatch to confirm.

    Returns:
        {"match_id": int, "confirmed": bool} or {"error": str}
    """
    async with async_session() as session:
        match = await session.get(ProductMatch, match_id)
        if not match:
            return {"error": f"ProductMatch {match_id} not found"}
        match.is_confirmed = True
        match.is_rejected = False
        await session.commit()
    return {"match_id": match_id, "confirmed": True}
```

- [ ] **Step 2: Verify import still works**

```bash
python -c "from shopping_agent.routes.mcp import mcp; print('MCP OK')"
```

- [ ] **Step 3: Count tools to verify all 19 are present**

```bash
python -c "
from shopping_agent.routes.mcp import mcp
import asyncio
tools = asyncio.run(mcp.get_tools())
print(f'Tools registered: {len(tools)}')
for t in sorted(tools, key=lambda x: x.name):
    print(f'  - {t.name}')
"
```

Expected: 19 tools listed.

- [ ] **Step 4: Commit**

```bash
git add src/shopping_agent/routes/mcp.py
git commit -m "feat: add product matching MCP tools (match_products, find_product_match, confirm_product_match)"
```

---

## Task 9: Mount MCP server in main.py

**Files:**
- Modify: `src/shopping_agent/main.py`

- [ ] **Step 1: Mount MCP at /mcp**

Add two lines to `src/shopping_agent/main.py` after the existing router includes:

```python
from .routes.mcp import mcp  # noqa: E402
app.mount("/mcp", mcp.http_app())
```

- [ ] **Step 2: Start the dev server and verify**

```bash
uvicorn shopping_agent.main:app --reload --host 0.0.0.0
```

In a second terminal:
```bash
curl -s http://localhost:8000/mcp
```

Expected: HTTP response (not 404). The MCP inspector or Claude Desktop can now connect.

- [ ] **Step 3: Run all tests**

```bash
pytest -v
```

Expected: all green

- [ ] **Step 4: Commit**

```bash
git add src/shopping_agent/main.py
git commit -m "feat: mount embedded MCP server at /mcp"
```

---

## Task 10: End-to-end verification

- [ ] **Step 1: Start server**

```bash
uvicorn shopping_agent.main:app --reload --host 0.0.0.0
```

- [ ] **Step 2: Run ruff and mypy**

```bash
ruff check .
mypy .
```

Fix any issues found.

- [ ] **Step 3: Connect MCP client**

Configure Claude Desktop or MCP inspector to connect to:
```
http://localhost:8000/mcp
```

- [ ] **Step 4: Test read-only tools manually**

In your MCP client, call:
- `get_auth_status(store="coles")` → should return auth status
- `get_predictions()` → should return prediction list
- `get_shopping_list()` → should return active list or null

- [ ] **Step 5: Final commit if any fixups needed**

```bash
git add -p
git commit -m "fix: address end-to-end verification issues"
```
