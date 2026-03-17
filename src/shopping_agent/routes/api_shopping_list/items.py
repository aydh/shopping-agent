"""Shopping list item operations — add, update quantity/store, remove, copy."""
from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...db_helpers import store_from_string
from ...models import Product, ShoppingList, ShoppingListItem, Store, ListStatus
from ...services.product_resolution import get_partner_product
from ...services.shopping_list import (
    choose_best_store,
    get_shopping_list_context as _shopping_list_context,
    remove_item,
    update_item_quantity,
    update_item_store,
)
from ...templating import templates

router = APIRouter()


@router.post("/items/{item_id}/quantity")
async def set_quantity(
    item_id: int,
    quantity: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    await update_item_quantity(session, item_id, quantity)
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.post("/items/{item_id}/store")
async def set_store(
    item_id: int,
    store: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    await update_item_store(session, item_id, store_from_string(store))
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.post("/items/add-product")
async def add_product_to_list(
    product_id: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Add a product (by id) to the active shopping list."""
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()

    if not shopping_list:
        return HTMLResponse('<span class="text-red-600 text-xs">No active list — generate one first.</span>')

    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse('<span class="text-red-600 text-xs">Product not found.</span>')

    # Determine the partner store (opposite of product's store)
    partner_store = "woolworths" if product.store == Store.COLES else "coles"
    partner_product_early = await get_partner_product(session, product_id, partner_store)
    partner_id = partner_product_early.id if partner_product_early else None

    # Already in list? Check both the product and its matched partner.
    candidate_ids = [product_id] + ([partner_id] if partner_id else [])
    existing = (await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == shopping_list.id,
            ShoppingListItem.product_id.in_(candidate_ids),
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )).scalars().first()
    if existing:
        existing.quantity += 1
        await session.commit()
        ctx = await _shopping_list_context(session)
        list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
        return HTMLResponse(
            f'<span class="text-green-600 text-xs">Qty updated ✓</span>'
            f'<div id="list-content" hx-swap-oob="innerHTML">{list_html}</div>'
        )

    # partner_product_early and partner_id already resolved above
    coles_price = None
    woolworths_price = None
    chosen_store = product.store

    if partner_product_early:
        coles_p = product if product.store == Store.COLES else partner_product_early
        ww_p = product if product.store == Store.WOOLWORTHS else partner_product_early
        coles_price = coles_p.current_price if coles_p else None
        woolworths_price = ww_p.current_price if ww_p else None
        chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
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
    ctx = await _shopping_list_context(session)
    list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(
        f'<span class="text-green-600 text-xs">Added ✓</span>'
        f'<div id="list-content" hx-swap-oob="innerHTML">{list_html}</div>'
    )


@router.delete("/items/{item_id}")
async def delete_item(item_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    await remove_item(session, item_id)
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.post("/copy/{source_list_id}")
async def copy_list(source_list_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Copy all items from a past list into the current active list."""
    active = (await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )).scalars().first()

    if not active:
        return HTMLResponse('<span class="text-red-600 text-sm">No active list — generate one first.</span>')

    source_items = (await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == source_list_id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )).scalars().all()

    added = 0
    for src in source_items:
        # Skip if already in active list
        existing = (await session.execute(
            select(ShoppingListItem).where(
                ShoppingListItem.shopping_list_id == active.id,
                ShoppingListItem.product_id == src.product_id,
                ShoppingListItem.is_removed == False,  # noqa: E712
            )
        )).scalars().first()
        if existing:
            continue

        # Resolve current prices from product table
        product = await session.get(Product, src.product_id)
        if not product:
            continue

        partner = await get_partner_product(session, src.product_id, product.store.value)

        coles_price = None
        woolworths_price = None
        if partner:
            coles_p = product if product.store == Store.COLES else partner
            ww_p = product if product.store == Store.WOOLWORTHS else partner
            coles_price = coles_p.current_price
            woolworths_price = ww_p.current_price
            chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
        else:
            if product.store == Store.COLES:
                coles_price = product.current_price
            else:
                woolworths_price = product.current_price
            chosen_store = product.store

        session.add(ShoppingListItem(
            shopping_list_id=active.id,
            product_id=src.product_id,
            quantity=src.quantity,
            coles_price=coles_price,
            woolworths_price=woolworths_price,
            chosen_store=chosen_store,
            is_user_added=True,
        ))
        added += 1

    await session.commit()
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)
