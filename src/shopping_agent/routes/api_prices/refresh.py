"""Price refresh background task and progress polling."""
import asyncio
import logging
from datetime import date as date_type

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import PRICE_REFRESH_CONCURRENCY
from ...database import async_session, get_session
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

# In-memory progress tracking: store_value -> {done, total, running}
_refresh_progress: dict[str, dict] = {}


async def _do_price_refresh(store_enum: Store) -> None:
    """Background task: refresh prices for all products of a given store."""
    scraper = ColesScraper() if store_enum == Store.COLES else WoolworthsScraper()
    concurrency = PRICE_REFRESH_CONCURRENCY
    key = store_enum.value

    async with async_session() as session:
        result = await session.execute(
            select(Product).where(Product.store == store_enum, Product.is_hidden == False)  # noqa: E712
        )
        products = list(result.scalars().all())

    _refresh_progress[key] = {"done": 0, "total": len(products), "running": True}
    logger.info("[PriceRefresh] Starting %s refresh for %d products", store_enum.value, len(products))

    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(product_id: int, store_product_id: str, product_name: str):
        async with sem:
            try:
                scraped = await scraper.get_product_price(store_product_id, product_name)
                async with async_session() as session:
                    product = await session.get(Product, product_id)
                    if product:
                        if scraped and scraped.current_price:
                            product.current_price = scraped.current_price
                            product.is_available = True
                            if scraped.unit_price:
                                product.unit_price = scraped.unit_price
                            if scraped.unit_price_measure:
                                product.unit_price_measure = scraped.unit_price_measure
                            if scraped.image_url:
                                product.image_url = scraped.image_url
                            # Upsert: update today's record if it exists, else insert
                            existing_ph = (await session.execute(
                                select(PriceHistory)
                                .where(PriceHistory.product_id == product_id)
                                .where(sqlfunc.date(PriceHistory.recorded_at) == date_type.today())
                            )).scalars().first()
                            if existing_ph:
                                existing_ph.price = scraped.current_price
                            else:
                                session.add(PriceHistory(product_id=product_id, store=store_enum, price=scraped.current_price))
                            # Sync active shopping list items that reference this product
                            # or items for the matched partner product
                            affected_product_ids = [product_id]
                            match = (await session.execute(
                                select(ProductMatch).where(
                                    (ProductMatch.product_a_id == product_id) | (ProductMatch.product_b_id == product_id),
                                    ProductMatch.is_rejected == False,  # noqa: E712
                                )
                            )).scalars().first()
                            if match:
                                partner_id = match.product_b_id if match.product_a_id == product_id else match.product_a_id
                                affected_product_ids.append(partner_id)
                            active_items = (await session.execute(
                                select(ShoppingListItem)
                                .join(ShoppingList, ShoppingListItem.shopping_list_id == ShoppingList.id)
                                .where(
                                    ShoppingList.status != ListStatus.ORDERED,
                                    ShoppingListItem.is_removed == False,  # noqa: E712
                                    ShoppingListItem.product_id.in_(affected_product_ids),
                                )
                            )).scalars().all()
                            for sli in active_items:
                                if store_enum == Store.COLES:
                                    sli.coles_price = scraped.current_price
                                else:
                                    sli.woolworths_price = scraped.current_price
                        elif scraped is not None and not scraped.is_available:
                            # Scraper explicitly says product is gone — mark unavailable
                            product.is_available = False
                            product.current_price = None
                        # else: scraper returned None (network/auth failure) — leave product unchanged
                        await session.commit()
                _refresh_progress[key]["done"] += 1
                return bool(scraped and scraped.current_price)
            except Exception as e:
                logger.error("[PriceRefresh] Error for product %s: %s", store_product_id, e)
                # Don't mark unavailable on exceptions — could be a transient network issue
            _refresh_progress[key]["done"] += 1
            return False

    results = await asyncio.gather(*[fetch_one(p.id, p.store_product_id, p.name) for p in products])
    updated = sum(results)
    _refresh_progress[key] = {"done": len(products), "total": len(products), "running": False, "updated": updated}
    logger.info("[PriceRefresh] %s done: %d/%d updated", store_enum.value, updated, len(products))


@router.post("/refresh/{store}")
async def refresh_prices(store: str, background_tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)):
    """Kick off a background price refresh for the given store."""
    store_enum = Store(store)
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
        f' hx-trigger="every 1s"'
        f' hx-target="#refresh-progress-{store_val}"'
        f' hx-swap="outerHTML">0/{count}</span>'
    )


@router.get("/refresh-progress/{store}")
async def refresh_progress(store: str):
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
            f' hx-trigger="every 1s"'
            f' hx-target="#refresh-progress-{store}"'
            f' hx-swap="outerHTML">{done}/{total}</span>'
        )
    updated = state.get("updated", done)
    response = HTMLResponse(
        f'<span class="text-green-600 text-sm">Done — {updated}/{total} updated</span>'
    )
    response.headers["HX-Refresh"] = "true"
    return response
