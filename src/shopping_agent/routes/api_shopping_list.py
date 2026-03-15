from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import ListStatus, Product, ProductMatch, ShoppingList, ShoppingListItem, Store
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
    await update_item_quantity(session, item_id, quantity)
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.post("/items/{item_id}/store")
async def set_store(
    item_id: int,
    store: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    await update_item_store(session, item_id, Store(store))
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.post("/set-store/{store}")
async def set_all_store(store: str, session: AsyncSession = Depends(get_session)):
    """Set all items in the active list to the given store."""
    store_enum = Store(store)
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if shopping_list:
        items_result = await session.execute(
            select(ShoppingListItem).where(
                ShoppingListItem.shopping_list_id == shopping_list.id,
                ShoppingListItem.is_removed == False,  # noqa: E712
            )
        )
        for item in items_result.scalars().all():
            item.chosen_store = store_enum
        await session.commit()

    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.post("/items/add-product")
async def add_product_to_list(
    product_id: int = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Add a product (by id) to the active shopping list."""
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()

    if not shopping_list:
        return HTMLResponse('<span class="text-red-600 text-xs">No active list — generate one first.</span>')

    # Already in list?
    existing = await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == shopping_list.id,
            ShoppingListItem.product_id == product_id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )
    if existing.scalars().first():
        return HTMLResponse('<span class="text-gray-400 text-xs">Already in list</span>')

    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse('<span class="text-red-600 text-xs">Product not found.</span>')

    # Look up cross-store prices via ProductMatch
    match_result = await session.execute(
        select(ProductMatch).where(
            (ProductMatch.product_a_id == product_id) | (ProductMatch.product_b_id == product_id),
            ProductMatch.is_rejected == False,  # noqa: E712
        )
    )
    match = match_result.scalars().first()

    coles_price = None
    woolworths_price = None
    chosen_store = product.store

    if match:
        pa = await session.get(Product, match.product_a_id)
        pb = await session.get(Product, match.product_b_id)
        coles_p = pa if pa.store == Store.COLES else pb
        ww_p = pa if pa.store == Store.WOOLWORTHS else pb
        coles_price = coles_p.current_price
        woolworths_price = ww_p.current_price
        if coles_price and woolworths_price:
            chosen_store = Store.COLES if coles_price <= woolworths_price else Store.WOOLWORTHS
        elif coles_price:
            chosen_store = Store.COLES
        else:
            chosen_store = Store.WOOLWORTHS
    else:
        if product.store == Store.COLES:
            coles_price = product.current_price
        else:
            woolworths_price = product.current_price

    session.add(ShoppingListItem(
        shopping_list_id=shopping_list.id,
        product_id=product_id,
        quantity=1,
        coles_price=coles_price,
        woolworths_price=woolworths_price,
        chosen_store=chosen_store,
        is_user_added=True,
    ))
    await session.commit()
    return HTMLResponse('<span class="text-green-600 text-xs">Added ✓</span>')


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
