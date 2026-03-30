"""Shopping list item operations — add, update quantity/store, remove, copy."""
from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import async_session, get_user_session_from_cookie, set_rls_claims
from ...db_helpers import store_from_string
from ...models import Product, ProductMatch, ShoppingList, ShoppingListItem, Store, ListStatus
from ...services.shopping_list import (
    add_item_to_list as _add_item_to_list,
    choose_best_store,
    get_shopping_list_context as _shopping_list_context,
    get_shopping_list_summary_context,
    remove_item,
    resolve_display_names,
    update_item_quantity,
    update_item_store,
)
from ...templating import templates

router = APIRouter()


async def _render_full_list_content(session: AsyncSession, user_id) -> str:
    """Render the full shopping-list body."""
    ctx = await _shopping_list_context(session, user_id)
    return templates.get_template("_shopping_list_content.html").render(**ctx)


async def _render_item_update_response(session: AsyncSession, user_id, item_id: int) -> HTMLResponse:
    """Render a single item row plus OOB summary updates after a small mutation."""
    summary = await get_shopping_list_summary_context(session, user_id)
    summary_ctx = {
        **summary,
        "coles_metrics": summary["store_metrics"]["coles"],
        "woolworths_metrics": summary["store_metrics"]["woolworths"],
    }
    parts = [
        templates.get_template("_sl_totals.html").render(**summary_ctx, oob=True),
        templates.get_template("_sl_meta.html").render(**summary, oob=True),
    ]

    item_result = await session.execute(
        select(ShoppingListItem)
        .options(selectinload(ShoppingListItem.product))
        .where(ShoppingListItem.id == item_id)
    )
    item = item_result.scalar_one_or_none()
    if item and not item.is_removed:
        _, _, store_products = await resolve_display_names(session, [item])
        sp = store_products.get(item.id, {})
        parts.insert(0, templates.get_template("_sl_item.html").render(
            item=item,
            coles_p=sp.get("coles"),
            ww_p=sp.get("woolworths"),
            oob=True,
        ))
        return HTMLResponse("".join(parts))

    list_html = await _render_full_list_content(session, user_id)
    parts.append(f'<div id="list-content" hx-swap-oob="innerHTML">{list_html}</div>')

    return HTMLResponse("".join(parts))


async def _render_item_delete_response(session: AsyncSession, user_id, item_id: int) -> HTMLResponse:
    """Render OOB updates after removing an item without rerendering the full table."""
    summary = await get_shopping_list_summary_context(session, user_id)
    summary_ctx = {
        **summary,
        "coles_metrics": summary["store_metrics"]["coles"],
        "woolworths_metrics": summary["store_metrics"]["woolworths"],
    }
    parts = [
        templates.get_template("_sl_totals.html").render(**summary_ctx, oob=True),
        templates.get_template("_sl_meta.html").render(**summary, oob=True),
    ]
    if summary["active_item_count"] == 0:
        list_html = await _render_full_list_content(session, user_id)
        parts.append(f'<div id="list-content" hx-swap-oob="innerHTML">{list_html}</div>')
        return HTMLResponse("".join(parts))

    parts.append(f'<div id="sl-item-{item_id}" hx-swap-oob="delete"></div>')
    return HTMLResponse("".join(parts))


@router.get("/product-search")
async def product_search(
    q: str = Query(default=""),
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
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
    quantity: int = Form(..., ge=0, le=99),
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    await update_item_quantity(session, item_id, quantity)
    async with async_session() as fresh:
        async with fresh.begin():
            await set_rls_claims(fresh, user.user_id)
            return await _render_item_update_response(fresh, user.user_id, item_id)


@router.post("/items/{item_id}/store")
async def set_store(
    item_id: int,
    store: str = Form(...),
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    await update_item_store(session, item_id, store_from_string(store))
    # service commits internally; read context in a fresh session
    async with async_session() as fresh:
        async with fresh.begin():
            await set_rls_claims(fresh, user.user_id)
            return await _render_item_update_response(fresh, user.user_id, item_id)


@router.post("/items/add-product")
async def add_product_to_list(
    product_id: int = Form(...),
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Add a product (by id) to the active shopping list."""
    item = await _add_item_to_list(session, user.user_id, product_id=product_id, quantity=1)
    if item is None:
        return HTMLResponse('<span class="text-red-600 text-xs">No active list or product not found.</span>')
    status = "Added ✓" if item.quantity == 1 else "Qty updated ✓"
    # service commits internally; read context in a fresh session
    async with async_session() as fresh:
        async with fresh.begin():
            await set_rls_claims(fresh, user.user_id)
            list_html = await _render_full_list_content(fresh, user.user_id)
    return HTMLResponse(
        f'<span class="text-green-600 text-xs">{status}</span>'
        f'<div id="list-content" hx-swap-oob="innerHTML">{list_html}</div>'
    )


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: int,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    await remove_item(session, item_id)
    async with async_session() as fresh:
        async with fresh.begin():
            await set_rls_claims(fresh, user.user_id)
            return await _render_item_delete_response(fresh, user.user_id, item_id)


@router.post("/copy/{source_list_id}")
async def copy_list(
    source_list_id: int,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
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
        return HTMLResponse(await _render_full_list_content(session, user.user_id))

    source_product_ids = [s.product_id for s in source_items]

    # Bulk-load existing active items on the active list (to update quantities instead of skip)
    existing_items_by_product: dict[int, ShoppingListItem] = {
        item.product_id: item for item in (await session.execute(
            select(ShoppingListItem).where(
                ShoppingListItem.shopping_list_id == active.id,
                ShoppingListItem.is_removed == False,  # noqa: E712
            )
        )).scalars().all()
    }

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
        existing = existing_items_by_product.get(src.product_id)
        if existing:
            existing.quantity = src.quantity
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

    # session.begin() context manager commits on exit; autoflush covers pending adds.
    return HTMLResponse(await _render_full_list_content(session, user.user_id))
