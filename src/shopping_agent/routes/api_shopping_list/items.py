"""Shopping list item operations — add, update quantity/store, remove, copy."""
from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...db_helpers import store_from_string
from ...models import Product, ProductMatch, ShoppingList, ShoppingListItem, Store, ListStatus
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


@router.get("/product-search")
async def product_search(
    q: str = Query(default=""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return an HTML dropdown of products matching the search query."""
    q = q.strip()
    if len(q) < 2:
        return HTMLResponse("")

    # Find product IDs already on the active list (not removed)
    active_list = (await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )).scalars().first()
    excluded_ids: set[int] = set()
    if active_list:
        existing_items = (await session.execute(
            select(ShoppingListItem.product_id)
            .where(
                ShoppingListItem.shopping_list_id == active_list.id,
                ShoppingListItem.is_removed == False,  # noqa: E712
            )
        )).scalars().all()
        excluded_ids = set(existing_items)

    results = (await session.execute(
        select(Product)
        .where(
            or_(
                Product.name.ilike(f"%{q}%"),
                Product.brand.ilike(f"%{q}%"),
            ),
            Product.is_available == True,  # noqa: E712
            Product.id.not_in(excluded_ids) if excluded_ids else True,
        )
        .order_by(Product.name)
        .limit(10)
    )).scalars().all()
    html = templates.get_template("_product_search_results.html").render(results=results)
    return HTMLResponse(html)


@router.post("/items/{item_id}/quantity")
async def set_quantity(
    item_id: int,
    quantity: int = Form(..., ge=1, le=99),
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

    try:
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
        status = "Added ✓"
    except IntegrityError:
        # Concurrent request already inserted this product — increment qty instead
        await session.rollback()
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
        status = "Qty updated ✓"
    ctx = await _shopping_list_context(session)
    list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(
        f'<span class="text-green-600 text-xs">{status}</span>'
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

    if source_list_id == active.id:
        return HTMLResponse('<span class="text-red-600 text-sm">Cannot copy a list onto itself.</span>')

    source_list = await session.get(ShoppingList, source_list_id)
    if not source_list or source_list.status != ListStatus.ORDERED:
        return HTMLResponse('<span class="text-red-600 text-sm">Source list not found or not a completed order.</span>')

    source_items = (await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == source_list_id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )).scalars().all()

    if not source_items:
        ctx = await _shopping_list_context(session)
        return HTMLResponse(templates.get_template("_shopping_list_content.html").render(**ctx))

    source_product_ids = [s.product_id for s in source_items]

    # Bulk-load products already on the active list (to skip duplicates)
    existing_product_ids: set[int] = set(
        (await session.execute(
            select(ShoppingListItem.product_id).where(
                ShoppingListItem.shopping_list_id == active.id,
                ShoppingListItem.is_removed == False,  # noqa: E712
            )
        )).scalars().all()
    )

    # Bulk-load all source products
    products_by_id: dict[int, Product] = {
        p.id: p for p in (await session.execute(
            select(Product).where(Product.id.in_(source_product_ids))
        )).scalars().all()
    }

    # Bulk-load all ProductMatch rows for source products
    match_rows = (await session.execute(
        select(ProductMatch).where(
            or_(
                ProductMatch.product_a_id.in_(source_product_ids),
                ProductMatch.product_b_id.in_(source_product_ids),
            ),
            ProductMatch.is_rejected == False,  # noqa: E712
        )
    )).scalars().all()

    # Build product_id -> partner_id map and collect all partner IDs to fetch
    partner_id_map: dict[int, int] = {}
    for m in match_rows:
        if m.product_a_id in products_by_id and m.product_a_id not in partner_id_map:
            partner_id_map[m.product_a_id] = m.product_b_id
        if m.product_b_id in products_by_id and m.product_b_id not in partner_id_map:
            partner_id_map[m.product_b_id] = m.product_a_id

    partner_ids = list(set(partner_id_map.values()) - set(products_by_id))
    partners_by_id: dict[int, Product] = {}
    if partner_ids:
        partners_by_id = {
            p.id: p for p in (await session.execute(
                select(Product).where(Product.id.in_(partner_ids))
            )).scalars().all()
        }

    # Process in memory — no per-item queries
    for src in source_items:
        if src.product_id in existing_product_ids:
            continue
        product = products_by_id.get(src.product_id)
        if not product:
            continue

        partner_id = partner_id_map.get(src.product_id)
        partner = partners_by_id.get(partner_id) if partner_id else None

        if partner:
            coles_p = product if product.store == Store.COLES else partner
            ww_p = product if product.store == Store.WOOLWORTHS else partner
            coles_price = coles_p.current_price
            woolworths_price = ww_p.current_price
            chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
        else:
            coles_price = product.current_price if product.store == Store.COLES else None
            woolworths_price = product.current_price if product.store == Store.WOOLWORTHS else None
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

    await session.commit()
    ctx = await _shopping_list_context(session)
    return HTMLResponse(templates.get_template("_shopping_list_content.html").render(**ctx))
