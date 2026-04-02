"""Price refresh service — fetch current prices for all products of a store."""
import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from ..config import COLES_PRICE_REFRESH_CONCURRENCY, WOOLWORTHS_PRICE_REFRESH_CONCURRENCY
from ..database import async_session
from ..models import ListStatus, PriceHistory, Product, ProductMatch, ShoppingList, ShoppingListItem, Store
from ..scrapers.registry import coles_scraper as _coles_scraper
from ..scrapers.registry import woolworths_scraper as _ww_scraper

logger = logging.getLogger(__name__)


async def do_price_refresh(
    store_enum: Store,
    progress_callback: Callable[[int, int], Awaitable[None] | None] | None = None,
) -> tuple[int, int]:
    """Refresh current prices for products of a given store.

    Fetches each product's current price concurrently (respecting per-store
    concurrency limits), updates Product.current_price, upserts today's
    PriceHistory entry, and syncs prices on active ShoppingListItems.

    Args:
        store_enum: The store to refresh prices for.
        progress_callback: Optional callback invoked with (done, total) after each product.

    Returns:
        Tuple of (updated_count, total_count) — number of products whose price
        was successfully fetched and total products processed.
    """
    async def _notify_progress(done: int, total: int) -> None:
        if progress_callback is None:
            return
        result = progress_callback(done, total)
        if inspect.isawaitable(result):
            await result

    scraper = _coles_scraper if store_enum == Store.COLES else _ww_scraper
    concurrency = COLES_PRICE_REFRESH_CONCURRENCY if store_enum == Store.COLES else WOOLWORTHS_PRICE_REFRESH_CONCURRENCY

    async with async_session() as session:
        result = await session.execute(
            select(Product).where(Product.store == store_enum)
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
        await _notify_progress(0, 0)
        return 0, 0

    logger.info("[PriceRefresh] Starting %s refresh for %d products", store_enum.value, len(products))
    sem = asyncio.Semaphore(concurrency)
    total_products = len(products)
    completed = 0
    progress_lock = asyncio.Lock()

    await _notify_progress(0, total_products)

    async def fetch_one(product: Product) -> bool:
        nonlocal completed
        async with sem:
            try:
                try:
                    # Pass timeout to httpx directly — asyncio.wait_for corrupts the
                    # connection pool when it cancels a mid-flight httpx request.
                    scraped = await scraper.get_product_price(
                        product.store_product_id, product.name, timeout=20.0
                    )
                except Exception as e:
                    logger.warning("[PriceRefresh] Request failed for %s: %s", product.store_product_id, e)
                    scraped = None
                async with async_session() as session:
                    db_product = await session.get(Product, product.id)
                    if db_product:
                        if scraped and scraped.current_price and scraped.is_available:
                            db_product.current_price = scraped.current_price
                            db_product.is_available = True
                            if scraped.name:
                                db_product.name = scraped.name
                            if scraped.brand:
                                db_product.brand = scraped.brand
                            if scraped.unit_size:
                                db_product.unit_size = scraped.unit_size
                            if scraped.unit_price:
                                db_product.unit_price = scraped.unit_price
                            if scraped.unit_price_measure:
                                db_product.unit_price_measure = scraped.unit_price_measure
                            if scraped.image_url:
                                db_product.image_url = scraped.image_url

                            # Check for existing price history for today (handles race condition
                            # where another concurrent task may have inserted first)
                            existing_ph = await session.execute(
                                select(PriceHistory).where(
                                    PriceHistory.product_id == product.id,
                                    PriceHistory.recorded_at >= today_start_utc,
                                    PriceHistory.recorded_at < tomorrow_start_utc,
                                )
                            )
                            existing_ph_obj = existing_ph.scalars().first()
                            if existing_ph_obj:
                                existing_ph_obj.price = scraped.current_price
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
            finally:
                async with progress_lock:
                    completed += 1
                    await _notify_progress(completed, total_products)
            return False

    results = await asyncio.gather(*[fetch_one(p) for p in products], return_exceptions=True)
    # Filter out exceptions; count only successful (bool) results
    updated = sum(r for r in results if isinstance(r, bool) and r)
    logger.info("[PriceRefresh] %s done: %d/%d updated", store_enum.value, updated, len(products))
    return updated, len(products)
