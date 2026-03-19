import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, StreamingResponse

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import delete, select

from ..database import async_session, get_session
from ..db_helpers import store_from_string
from ..models import Order, OrderItem, PriceHistory, Product, Store
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper
from ..services.order_sync import sync_orders
from ..templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/sync-stream/{store}")
async def sync_orders_stream(store: str) -> StreamingResponse:
    """SSE endpoint: fetches orders and streams each row as it's saved."""
    store_enum = store_from_string(store)
    scraper = coles_scraper if store_enum == Store.COLES else woolworths_scraper

    async def generate():
        if not await scraper.is_authenticated():
            yield f"event: error\ndata: {json.dumps({'message': f'Not connected to {store_enum.value.title()}'})}\n\n"
            return

        yield f"event: fetching\ndata: {{}}\n\n"

        new_count = 0
        fetched = 0
        try:
            async for scraped_order in scraper.stream_order_history(limit=100):
                fetched += 1
                yield f"event: progress\ndata: {json.dumps({'fetched': fetched})}\n\n"
                try:
                    async with async_session() as session:
                        count = await sync_orders(session, [scraped_order], store_enum)
                        new_count += count
                        result = await session.execute(
                            select(Order)
                            .options(selectinload(Order.items))
                            .where(
                                Order.store_order_id == scraped_order.store_order_id,
                                Order.store == store_enum,
                            )
                        )
                        order = result.scalars().first()
                    if order:
                        row_html = templates.env.get_template("partials/order_row.html").render(order=order)
                        yield f"event: order\ndata: {json.dumps({'html': row_html, 'is_new': count > 0})}\n\n"
                except Exception:
                    logger.exception("Unexpected error syncing order %s — skipping", scraped_order.store_order_id)
                    yield f"event: error\ndata: {json.dumps({'message': 'Failed to sync order — see server logs'})}\n\n"
                    continue
        except Exception:
            logger.exception("Unexpected error during order stream for %s", store)
            yield f"event: error\ndata: {json.dumps({'message': 'Sync failed — see server logs'})}\n\n"
            return

        yield f"event: done\ndata: {json.dumps({'new_count': new_count})}\n\n"

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/purge/{store}")
async def purge_store_orders(store: str, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    store_enum = store_from_string(store)

    order_subq = select(Order.id).where(Order.store == store_enum).scalar_subquery()
    await session.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_subq)))
    result = await session.execute(delete(Order).where(Order.store == store_enum))

    product_subq = select(Product.id).where(Product.store == store_enum).scalar_subquery()
    await session.execute(delete(PriceHistory).where(PriceHistory.product_id.in_(product_subq)))

    await session.commit()
    label = store_enum.value.capitalize()
    return HTMLResponse(
        f'<div class="text-orange-600 text-sm mt-2">Purged {result.rowcount} {label} orders from the database.</div>'
    )


@router.get("/{order_id}/items")
async def get_order_items(order_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    result = await session.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
        .where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()

    if not order or not order.items:
        return HTMLResponse('<p class="text-gray-400 text-sm">No items found.</p>')

    html = templates.env.get_template("fragments/_order_items_table.html").render(
        order=order, order_id=order_id
    )
    return HTMLResponse(html)
