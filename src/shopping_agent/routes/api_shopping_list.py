from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import ShoppingList, ShoppingListItem, Store
from ..services.shopping_list import (
    confirm_list,
    generate_shopping_list,
    get_active_list,
    remove_item,
    update_item_quantity,
    update_item_store,
)
from ..templating import templates
from .views import _shopping_list_context

router = APIRouter()


@router.post("/generate")
async def generate(session: AsyncSession = Depends(get_session)):
    await generate_shopping_list(session)
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.post("/items/{item_id}/quantity")
async def set_quantity(
    item_id: int,
    quantity: int = Form(...),
    session: AsyncSession = Depends(get_session),
):
    item = await update_item_quantity(session, item_id, quantity)
    if not item:
        return HTMLResponse("")

    # Return updated row (simplified - in production, render the partial template)
    return HTMLResponse(
        f'<tr id="item-{item.id}"><td colspan="7" class="px-6 py-2 text-sm text-green-600">'
        f'Updated quantity to {item.quantity}. <a href="/shopping-list" class="underline">Reload</a>'
        f"</td></tr>"
    )


@router.post("/items/{item_id}/store")
async def set_store(
    item_id: int,
    store: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    await update_item_store(session, item_id, Store(store))
    return HTMLResponse(
        '<div class="text-green-600 text-xs">Store updated. '
        '<a href="/shopping-list" class="underline">Reload</a> for totals.</div>'
    )


@router.delete("/items/{item_id}")
async def delete_item(item_id: int, session: AsyncSession = Depends(get_session)):
    await remove_item(session, item_id)
    return HTMLResponse("")


@router.post("/confirm/{list_id}")
async def confirm(list_id: int, session: AsyncSession = Depends(get_session)):
    await confirm_list(session, list_id)
    return RedirectResponse("/confirm", status_code=303)


@router.delete("/purge")
async def purge_shopping_lists(session: AsyncSession = Depends(get_session)):
    items = await session.execute(delete(ShoppingListItem))
    lists = await session.execute(delete(ShoppingList))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {lists.rowcount} lists and {items.rowcount} items.</span>'
    )
