from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select

from ..database import get_session
from ..models import Order, OrderItem, Store
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper
from ..services.order_sync import sync_orders

router = APIRouter()


@router.post("/sync/{store}")
async def sync_store_orders(store: str, session: AsyncSession = Depends(get_session)):
    store_enum = Store(store)
    scraper = coles_scraper if store_enum == Store.COLES else woolworths_scraper

    if not await scraper.is_authenticated():
        return HTMLResponse(
            '<div class="text-red-600 text-sm mt-2">Not logged in. Please login first in Settings.</div>'
        )

    try:
        scraped_orders = await scraper.get_order_history()
        new_count = await sync_orders(session, scraped_orders, store_enum)
        return HTMLResponse(
            f'<div class="text-green-600 text-sm mt-2">Synced {new_count} new orders from {store}.</div>'
        )
    except Exception as e:
        return HTMLResponse(
            f'<div class="text-red-600 text-sm mt-2">Sync failed: {e}</div>'
        )


@router.get("/{order_id}/items")
async def get_order_items(order_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
        .where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()

    if not order or not order.items:
        return HTMLResponse('<p class="text-gray-400 text-sm">No items found.</p>')

    rows = []
    for item in order.items:
        rows.append(
            f"""<tr>
                <td class="px-4 py-2 text-sm text-gray-900">{item.product.name}</td>
                <td class="px-4 py-2 text-sm text-gray-500">{item.quantity}</td>
                <td class="px-4 py-2 text-sm text-gray-500">${item.price_paid:.2f}</td>
            </tr>"""
        )

    html = f"""
    <table class="min-w-full text-sm">
        <thead><tr>
            <th class="px-4 py-2 text-left text-xs text-gray-500">Product</th>
            <th class="px-4 py-2 text-left text-xs text-gray-500">Qty</th>
            <th class="px-4 py-2 text-left text-xs text-gray-500">Price</th>
        </tr></thead>
        <tbody>{"".join(rows)}</tbody>
    </table>
    <script>
        document.getElementById('order-detail-row-{order_id}').classList.remove('hidden');
    </script>
    """
    return HTMLResponse(html)
