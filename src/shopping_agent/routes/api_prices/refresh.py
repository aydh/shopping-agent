"""Price refresh background task and progress polling."""
import asyncio
import logging
from datetime import date as date_type
from typing import TypedDict

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...config import COLES_PRICE_REFRESH_CONCURRENCY, WOOLWORTHS_PRICE_REFRESH_CONCURRENCY, settings
from ...database import async_session, get_session
from ...db_helpers import store_from_string, visible_products_query
from ...models import (
    ListStatus,
    PriceHistory,
    Product,
    ProductMatch,
    ShoppingList,
    ShoppingListItem,
    Store,
)
from ...scrapers.coles import ColesScraper
from ...scrapers.woolworths import WoolworthsScraper

router = APIRouter()
logger = logging.getLogger(__name__)


class RefreshState(TypedDict, total=False):
    done: int
    total: int
    running: bool
    updated: int


# In-memory progress tracking: store_value -> {done, total, running}
_refresh_progress: dict[str, RefreshState] = {}


async def _do_price_refresh(store_enum: Store) -> None:
    """Background task: refresh prices for all products of a given store."""
    scraper = ColesScraper() if store_enum == Store.COLES else WoolworthsScraper()
    concurrency = COLES_PRICE_REFRESH_CONCURRENCY if store_enum == Store.COLES else WOOLWORTHS_PRICE_REFRESH_CONCURRENCY
    key = store_enum.value

    async with async_session() as session:
        result = await session.execute(
            visible_products_query().where(Product.store == store_enum)
        )
        products = list(result.scalars().all())
        product_ids = [p.id for p in products]
        product_map = {p.id: p for p in products}

        # Pre-load today's price history entries
        today = date_type.today()
        ph_rows = await session.execute(
            select(PriceHistory)
            .where(
                PriceHistory.product_id.in_(product_ids),
                sqlfunc.date(PriceHistory.recorded_at) == today,
            )
        )
        today_ph: dict[int, PriceHistory] = {ph.product_id: ph for ph in ph_rows.scalars()}

        # Pre-load partner products via ProductMatch
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

        # Pre-load active shopping list items for affected products
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
        items_by_product: dict[int, list[ShoppingListItem]] = {}
        for sli in sli_rows.scalars():
            items_by_product.setdefault(sli.product_id, []).append(sli)

    _refresh_progress[key] = {"done": 0, "total": len(products), "running": True}
    logger.info("[PriceRefresh] Starting %s refresh for %d products", store_enum.value, len(products))

    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(product: Product):
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

                            # Upsert today's price history (pre-loaded — no extra query)
                            existing_ph = today_ph.get(product.id)
                            if existing_ph:
                                merged_ph = await session.merge(existing_ph)
                                merged_ph.price = scraped.current_price
                            else:
                                session.add(PriceHistory(
                                    product_id=product.id, store=store_enum, price=scraped.current_price
                                ))

                            # Sync active shopping list items (pre-loaded — no extra queries)
                            affected_ids = [product.id]
                            partner = partner_map.get(product.id)
                            if partner:
                                affected_ids.append(partner.id)
                            for pid in affected_ids:
                                for sli in items_by_product.get(pid, []):
                                    merged_sli = await session.merge(sli)
                                    if store_enum == Store.COLES:
                                        merged_sli.coles_price = scraped.current_price
                                    else:
                                        merged_sli.woolworths_price = scraped.current_price

                        elif scraped is not None and not scraped.is_available:
                            db_product.is_available = False
                            db_product.current_price = None
                            # Clear price on any shopping list items for this product
                            affected_ids = [product.id]
                            partner = partner_map.get(product.id)
                            if partner:
                                affected_ids.append(partner.id)
                            for pid in affected_ids:
                                for sli in items_by_product.get(pid, []):
                                    merged_sli = await session.merge(sli)
                                    if store_enum == Store.COLES:
                                        merged_sli.coles_price = None
                                    else:
                                        merged_sli.woolworths_price = None
                        await session.commit()
                _refresh_progress[key]["done"] += 1
                return bool(scraped and scraped.current_price)
            except Exception as e:
                logger.error("[PriceRefresh] Error for product %s: %s", product.store_product_id, e)
            _refresh_progress[key]["done"] += 1
            return False

    try:
        results = await asyncio.gather(*[fetch_one(p) for p in products])
        updated = sum(results)
        logger.info("[PriceRefresh] %s done: %d/%d updated", store_enum.value, updated, len(products))
    except Exception:
        logger.exception("[PriceRefresh] Unexpected error during %s refresh", store_enum.value)
        updated = _refresh_progress[key].get("done", 0)
    finally:
        _refresh_progress[key] = {"done": len(products), "total": len(products), "running": False, "updated": updated}


@router.post("/refresh/{store}")
async def refresh_prices(store: str, background_tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Kick off a background price refresh for the given store."""
    store_enum = store_from_string(store)
    scraper = ColesScraper() if store_enum == Store.COLES else WoolworthsScraper()

    if not await scraper.is_authenticated():
        return HTMLResponse(f'<span class="text-red-600 text-sm">Not connected to {store_enum.value.title()}.</span>')

    result = await session.execute(select(Product).where(Product.store == store_enum))
    count = len(result.scalars().all())
    if not count:
        return HTMLResponse(f'<span class="text-yellow-600 text-sm">No {store_enum.value.title()} products.</span>')

    background_tasks.add_task(_do_price_refresh, store_enum)
    store_val = store_enum.value
    return HTMLResponse(
        f'<span id="refresh-progress-{store_val}" class="text-blue-600 text-sm"'
        f' hx-get="/api/prices/refresh-progress/{store_val}"'
        f' hx-trigger="every {settings.price_refresh_poll_interval_ms}ms"'
        f' hx-target="#refresh-progress-{store_val}"'
        f' hx-swap="outerHTML">0/{count}</span>'
    )


@router.get("/refresh-progress/{store}")
async def refresh_progress(store: str) -> HTMLResponse:
    """Poll endpoint for price refresh progress."""
    state = _refresh_progress.get(store)
    if not state:
        return HTMLResponse("")
    done = state["done"]
    total = state["total"]
    running = state["running"]
    if running:
        return HTMLResponse(
            f'<span id="refresh-progress-{store}" class="text-blue-600 text-sm"'
            f' hx-get="/api/prices/refresh-progress/{store}"'
            f' hx-trigger="every {settings.price_refresh_poll_interval_ms}ms"'
            f' hx-target="#refresh-progress-{store}"'
            f' hx-swap="outerHTML">{done}/{total}</span>'
        )
    updated = state.get("updated", done)
    response = HTMLResponse(
        f'<span class="text-green-600 text-sm">Done — {updated}/{total} updated</span>'
    )
    response.headers["HX-Refresh"] = "true"
    return response
