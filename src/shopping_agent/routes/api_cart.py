import json

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..database import async_session, get_session
from ..models import ListStatus, ShoppingList, ShoppingListItem, Store
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper
from ..services.cart import _resolve_store_product_id, add_to_cart

router = APIRouter()


@router.get("/stream/{store}")
async def add_to_cart_stream(store: str):
    """SSE endpoint: adds items to cart one at a time, streaming per-item results."""
    store_enum = Store(store)
    scraper = coles_scraper if store_enum == Store.COLES else woolworths_scraper

    async def generate():
        async with async_session() as session:
            result = await session.execute(
                select(ShoppingList)
                .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
                .where(ShoppingList.status == ListStatus.CONFIRMED)
                .order_by(ShoppingList.created_at.desc())
            )
            shopping_list = result.scalars().first()
            if not shopping_list:
                yield f"event: done\ndata: {json.dumps({'error': 'No confirmed list'})}\n\n"
                return

            items_to_process = []
            for item in shopping_list.items:
                if item.is_removed or item.chosen_store != store_enum:
                    continue
                spid = await _resolve_store_product_id(session, item.product, store_enum)
                if spid:
                    items_to_process.append((item.id, str(spid), item.quantity))

        succeeded = 0
        failed_ids = []
        for item_id, spid, quantity in items_to_process:
            results = await scraper.add_to_cart([(spid, quantity)])
            success = results.get(spid, False)
            async with async_session() as session:
                item = await session.get(ShoppingListItem, item_id)
                if item and success:
                    item.is_ordered = True
                    await session.commit()
            if success:
                succeeded += 1
            else:
                failed_ids.append(item_id)
            yield f"event: item\ndata: {json.dumps({'item_id': item_id, 'success': success})}\n\n"

        cart_url = await scraper.get_cart_url()
        yield f"event: done\ndata: {json.dumps({'succeeded': succeeded, 'total': len(items_to_process), 'cart_url': cart_url, 'failed_ids': failed_ids})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/add/{store}")
async def add_items_to_cart(store: str, session: AsyncSession = Depends(get_session)):
    store_enum = Store(store)
    result = await add_to_cart(session, store_enum)

    failed_ids = result.get("failed_item_ids", [])
    highlight_js = ""
    if failed_ids:
        ids_js = ", ".join(f"'item-row-{i}'" for i in failed_ids)
        highlight_js = f"""<script>
[{ids_js}].forEach(id => {{
    const el = document.getElementById(id);
    if (el) el.classList.add('bg-yellow-50');
}});
</script>"""

    cart_url = result.get("cart_url", "#")
    store_label = store.title()

    if result.get("count", 0) > 0 or result.get("success"):
        status_html = (
            f'<div class="p-3 bg-green-50 border border-green-200 text-green-800 text-sm rounded mt-2">'
            f'{result["message"]}. '
            f'<a href="{cart_url}" target="_blank" class="underline font-medium">'
            f"Go to {store_label} &rarr;</a> then click the trolley icon in the top right.</div>"
        )
        if failed_ids:
            status_html = (
                f'<div class="p-3 bg-yellow-50 border border-yellow-200 text-yellow-800 text-sm rounded mt-2">'
                f'{result["message"]}. {len(failed_ids)} item(s) highlighted below failed to add. '
                f'<a href="{cart_url}" target="_blank" class="underline font-medium">'
                f"Go to {store_label} &rarr;</a></div>"
            )
        return HTMLResponse(status_html + highlight_js)

    return HTMLResponse(
        f'<div class="p-3 bg-red-50 border border-red-200 text-red-800 text-sm rounded mt-2">'
        f'{result.get("error") or result["message"]}</div>'
    )
