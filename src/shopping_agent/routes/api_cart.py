import json

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..auth import CurrentUser, get_current_user_from_cookie
from ..database import async_session, get_user_session_from_cookie, set_rls_claims
from ..db_helpers import store_from_string
from ..models import ListStatus, ProductMatch, ShoppingList, ShoppingListItem, Store
from ..scrapers.registry import get_scraper
from ..services.cart import _resolve_store_product_id, add_to_cart
from ..templating import templates

router = APIRouter()


@router.get("/stream/{store}")
async def add_to_cart_stream(
    store: str,
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> StreamingResponse:
    """SSE endpoint: adds items to cart one at a time, streaming per-item results."""
    store_enum = store_from_string(store)
    scraper = get_scraper(user.user_id, store_enum)

    async def generate():
        async with async_session() as session:
            async with session.begin():
                await set_rls_claims(session, user.user_id)
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

                # Build partner map for product resolution
                product_ids = [item.product.id for item in shopping_list.items]
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
                partner_map: dict[int, str] = {}
                for m in match_rows.scalars():
                    if m.product_a_id in product_ids:
                        partner_map[m.product_a_id] = m.product_b
                    if m.product_b_id in product_ids:
                        partner_map[m.product_b_id] = m.product_a

                items_to_process = []
                for item in shopping_list.items:
                    if item.is_removed or item.chosen_store != store_enum:
                        continue
                    spid = _resolve_store_product_id(item.product, store_enum, partner_map)
                    if spid:
                        items_to_process.append((item.id, str(spid), item.quantity))

        succeeded = 0
        failed_ids = []
        for item_id, spid, quantity in items_to_process:
            results = await scraper.add_to_cart([(spid, quantity)])
            success = results.get(spid, False)
            async with async_session() as session:
                async with session.begin():
                    await set_rls_claims(session, user.user_id)
                    item = await session.get(ShoppingListItem, item_id)
                    if item and success:
                        item.is_ordered = True
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
async def add_items_to_cart(
    store: str,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    store_enum = store_from_string(store)
    coles_scraper = get_scraper(user.user_id, Store.COLES)
    woolworths_scraper = get_scraper(user.user_id, Store.WOOLWORTHS)
    result = await add_to_cart(session, store_enum, coles_scraper, woolworths_scraper)

    failed_ids = result.get("failed_item_ids", [])
    highlight_js = templates.env.get_template("fragments/_cart_highlight.html").render(
        failed_ids=failed_ids
    )

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
