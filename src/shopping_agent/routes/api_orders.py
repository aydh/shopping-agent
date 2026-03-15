from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import delete, select

from ..database import async_session, get_session
from ..models import Order, OrderItem, PriceHistory, Product, Store
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper
from ..services.order_sync import sync_orders
from ..services.price_comparison import match_unmatched_products

router = APIRouter()

# In-memory progress tracking: store_value -> {phase, fetched, new_count, matched, running, error}
_sync_progress: dict[str, dict] = {}


def _progress_span(store: str, state: dict) -> str:
    sid = f"sync-progress-{store}"
    if state.get("running"):
        phase = state.get("phase", "running")
        fetched = state.get("fetched", 0)
        msg = f"Fetching orders… {fetched} found" if phase == "fetching" else f"Saving {fetched} orders…" if phase == "saving" else f"Matching products…"
        return (
            f'<span id="{sid}" class="text-blue-600 text-sm"'
            f' hx-get="/api/orders/sync-progress/{store}"'
            f' hx-trigger="every 1s" hx-target="#{sid}" hx-swap="outerHTML">{msg}</span>'
        )
    if state.get("error"):
        return f'<span id="{sid}" class="text-red-600 text-sm">Sync failed: {state["error"]}</span>'
    new_count = state.get("new_count", 0)
    matched = state.get("matched", 0)
    match_msg = f", {matched} matched" if matched else ""
    return f'<span id="{sid}" class="text-green-600 text-sm">Done — {new_count} new orders{match_msg}</span>'


async def _do_sync(store_enum: Store) -> None:
    key = store_enum.value
    scraper = coles_scraper if store_enum == Store.COLES else woolworths_scraper
    _sync_progress[key] = {"running": True, "phase": "fetching", "fetched": 0}
    try:
        scraped_orders = await scraper.get_order_history(limit=100)
        _sync_progress[key].update({"phase": "saving", "fetched": len(scraped_orders)})

        async with async_session() as session:
            new_count = await sync_orders(session, scraped_orders, store_enum)

        _sync_progress[key].update({"phase": "matching", "new_count": new_count})

        async with async_session() as session:
            matched_count = await match_unmatched_products(session, store_enum)

        _sync_progress[key] = {"running": False, "new_count": new_count, "matched": matched_count}
    except Exception as e:
        _sync_progress[key] = {"running": False, "error": str(e)}


@router.post("/sync/{store}")
async def sync_store_orders(store: str, background_tasks: BackgroundTasks):
    store_enum = Store(store)
    scraper = coles_scraper if store_enum == Store.COLES else woolworths_scraper

    if not await scraper.is_authenticated():
        return HTMLResponse(
            f'<span class="text-red-600 text-sm">Not connected to {store_enum.value.title()}.</span>'
        )

    background_tasks.add_task(_do_sync, store_enum)
    _sync_progress[store_enum.value] = {"running": True, "phase": "fetching", "fetched": 0}
    return HTMLResponse(_progress_span(store_enum.value, _sync_progress[store_enum.value]))


@router.get("/sync-progress/{store}")
async def sync_progress(store: str):
    state = _sync_progress.get(store)
    if not state:
        return HTMLResponse("")
    return HTMLResponse(_progress_span(store, state))


@router.delete("/purge/{store}")
async def purge_store_orders(store: str, session: AsyncSession = Depends(get_session)):
    store_enum = Store(store)

    # Fetch order IDs for this store so we can delete items first
    result = await session.execute(
        select(Order.id).where(Order.store == store_enum)
    )
    order_ids = [row[0] for row in result.all()]

    if order_ids:
        await session.execute(
            delete(OrderItem).where(OrderItem.order_id.in_(order_ids))
        )
        await session.execute(
            delete(Order).where(Order.store == store_enum)
        )

    # Also clear price history for this store's products
    product_ids_result = await session.execute(
        select(Product.id).where(Product.store == store_enum)
    )
    product_ids = [row[0] for row in product_ids_result.all()]
    if product_ids:
        await session.execute(
            delete(PriceHistory).where(PriceHistory.product_id.in_(product_ids))
        )

    await session.commit()
    count = len(order_ids) if order_ids else 0

    label = store_enum.value.capitalize()
    return HTMLResponse(
        f'<div class="text-orange-600 text-sm mt-2">Purged {count} {label} orders from the database.</div>'
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
