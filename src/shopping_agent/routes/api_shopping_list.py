from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import ListStatus, Product, ProductMatch, ShoppingList, ShoppingListItem, Store
from ..services.price_comparison import build_price_map
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


def _list_header_oob(shopping_list) -> str:
    """Render an OOB swap for #list-header reflecting the current list state."""
    has_list = shopping_list is not None
    name = shopping_list.name if has_list else None
    title = name if name else "Shopping List"
    new_cls = "bg-gray-200 text-gray-400 cursor-not-allowed" if has_list else "bg-blue-600 text-white hover:bg-blue-700"
    pred_cls = "bg-gray-200 text-gray-400 cursor-not-allowed" if not has_list else "bg-green-600 text-white hover:bg-green-700"
    new_disabled = "disabled" if has_list else ""
    pred_disabled = "disabled" if not has_list else ""
    return f"""<div id="list-header" hx-swap-oob="innerHTML">
    <h1 class="text-2xl font-bold text-gray-900">{title}</h1>
    <div class="flex flex-wrap gap-2 items-center">
        <button hx-post="/api/shopping-list/new" hx-target="#list-content" hx-swap="innerHTML"
                {new_disabled} class="px-4 py-2 text-sm rounded {new_cls}">New List</button>
        <button hx-post="/api/shopping-list/add-predictions" hx-target="#list-content" hx-swap="innerHTML"
                hx-indicator="#pred-spinner" {pred_disabled}
                class="px-4 py-2 text-sm rounded {pred_cls}">Add Predicted Items</button>
        <span id="pred-spinner" class="htmx-indicator text-gray-400 self-center text-sm">adding...</span>
        {'<button hx-delete="/api/shopping-list/current" hx-target="#list-content" hx-swap="innerHTML" class="px-4 py-2 text-sm rounded bg-red-100 text-red-600 hover:bg-red-200">Delete List</button>' if has_list else ''}
    </div>
</div>"""


@router.delete("/current")
async def delete_current_list(session: AsyncSession = Depends(get_session)):
    """Delete the current active (non-ordered) shopping list."""
    shopping_list = (await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )).scalars().first()

    if shopping_list:
        await session.execute(
            delete(ShoppingListItem).where(ShoppingListItem.shopping_list_id == shopping_list.id)
        )
        await session.delete(shopping_list)
        await session.commit()

    ctx = await _shopping_list_context(session)
    list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(list_html + _list_header_oob(None))


@router.post("/new")
async def new_list(session: AsyncSession = Depends(get_session)):
    """Create a new empty shopping list (disabled if one already exists)."""
    existing = (await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )).scalars().first()

    if existing:
        return HTMLResponse("")

    from datetime import date
    today = date.today()
    # "Week of Mon DD MMM YYYY"
    name = f"Week of {today.strftime('%d %b %Y')}"
    shopping_list = ShoppingList(name=name, target_date=today, status=ListStatus.DRAFT)
    session.add(shopping_list)
    await session.commit()

    ctx = await _shopping_list_context(session)
    list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(list_html + _list_header_oob(ctx["shopping_list"]))


@router.post("/add-predictions")
async def add_predictions(session: AsyncSession = Depends(get_session)):
    """Add predicted items to the current active list without replacing existing items."""
    from datetime import date
    from ..models import ConsumptionPrediction
    from ..services.prediction import generate_candidates
    from sqlalchemy.orm import selectinload

    active = (await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )).scalars().first()

    if not active:
        ctx = await _shopping_list_context(session)
        list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
        return HTMLResponse(list_html)

    predictions = list((await session.execute(
        select(ConsumptionPrediction).options(selectinload(ConsumptionPrediction.product))
    )).scalars().all())

    candidates = generate_candidates(predictions, target_date=date.today(), lookahead_days=7)

    # Build price map from matches
    from sqlalchemy.orm import selectinload as sil
    matches = (await session.execute(
        select(ProductMatch).options(sil(ProductMatch.product_a), sil(ProductMatch.product_b))
    )).scalars().all()
    price_map = build_price_map(list(matches))

    # Existing product ids in the active list
    existing_ids = {r[0] for r in (await session.execute(
        select(ShoppingListItem.product_id).where(
            ShoppingListItem.shopping_list_id == active.id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )).all()}

    for candidate in candidates:
        if candidate.product_id in existing_ids:
            continue
        product = await session.get(Product, candidate.product_id)
        if not product:
            continue
        if candidate.product_id in price_map:
            prices = price_map[candidate.product_id]
            coles_price = prices["coles_price"]
            woolworths_price = prices["woolworths_price"]
            if coles_price and woolworths_price:
                chosen_store = Store.COLES if coles_price <= woolworths_price else Store.WOOLWORTHS
            else:
                chosen_store = product.store
        else:
            coles_price = product.current_price if product.store == Store.COLES else None
            woolworths_price = product.current_price if product.store == Store.WOOLWORTHS else None
            chosen_store = product.store
        session.add(ShoppingListItem(
            shopping_list_id=active.id,
            product_id=candidate.product_id,
            quantity=candidate.quantity,
            reason=candidate.reason,
            coles_price=coles_price,
            woolworths_price=woolworths_price,
            chosen_store=chosen_store,
        ))

    await session.commit()
    ctx = await _shopping_list_context(session)
    list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(list_html)


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

    # Already in list? Check both the product and its matched partner.
    match_result = await session.execute(
        select(ProductMatch).where(
            (ProductMatch.product_a_id == product_id) | (ProductMatch.product_b_id == product_id),
            ProductMatch.is_rejected == False,  # noqa: E712
        )
    )
    match = match_result.scalars().first()
    partner_id = None
    if match:
        partner_id = match.product_b_id if match.product_a_id == product_id else match.product_a_id

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

    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse('<span class="text-red-600 text-xs">Product not found.</span>')

    # match and partner_id already resolved above
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
    ctx = await _shopping_list_context(session)
    list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(
        f'<span class="text-green-600 text-xs">Added ✓</span>'
        f'<div id="list-content" hx-swap-oob="innerHTML">{list_html}</div>'
    )


@router.delete("/items/{item_id}")
async def delete_item(item_id: int, session: AsyncSession = Depends(get_session)):
    await remove_item(session, item_id)
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.post("/confirm/{list_id}")
async def confirm(list_id: int, session: AsyncSession = Depends(get_session)):
    await confirm_list(session, list_id)
    return RedirectResponse("/confirm", status_code=303)


@router.post("/close/{list_id}")
async def close_list(list_id: int, session: AsyncSession = Depends(get_session)):
    """Mark the shopping list as ordered (closed)."""
    shopping_list = await session.get(ShoppingList, list_id)
    if shopping_list:
        shopping_list.status = ListStatus.ORDERED
        await session.commit()
    return RedirectResponse("/shopping-list", status_code=303)


@router.post("/submit-store/{store}")
async def submit_store(store: str, session: AsyncSession = Depends(get_session)):
    """Set all items to a single store, confirm, and redirect to review."""
    store_enum = Store(store)
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if not shopping_list:
        return RedirectResponse("/shopping-list", status_code=303)
    items_result = await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == shopping_list.id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )
    for item in items_result.scalars().all():
        item.chosen_store = store_enum
    shopping_list.status = ListStatus.CONFIRMED
    await session.commit()
    return RedirectResponse("/confirm", status_code=303)


@router.post("/submit-split")
async def submit_split(session: AsyncSession = Depends(get_session)):
    """Set each item to its cheapest available store, confirm, and redirect to review."""
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if not shopping_list:
        return RedirectResponse("/shopping-list", status_code=303)
    items_result = await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == shopping_list.id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )
    for item in items_result.scalars().all():
        if item.coles_price and item.woolworths_price:
            item.chosen_store = Store.COLES if item.coles_price <= item.woolworths_price else Store.WOOLWORTHS
        elif item.coles_price:
            item.chosen_store = Store.COLES
        else:
            item.chosen_store = Store.WOOLWORTHS
    shopping_list.status = ListStatus.CONFIRMED
    await session.commit()
    return RedirectResponse("/confirm", status_code=303)


@router.get("/details/{list_id}")
async def list_details(list_id: int, session: AsyncSession = Depends(get_session)):
    """Return an HTML fragment listing all items in a past shopping list."""
    items_result = await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == list_id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )
    items = items_result.scalars().all()

    rows = ""
    for item in items:
        product = await session.get(Product, item.product_id)
        if not product:
            continue
        name = product.name
        cp = f"${item.coles_price:.2f}" if item.coles_price else "—"
        wp = f"${item.woolworths_price:.2f}" if item.woolworths_price else "—"
        rows += f"""
        <tr>
            <td class="px-4 py-2 text-sm text-gray-900">{name}</td>
            <td class="px-4 py-2 text-sm text-gray-500 text-center">{item.quantity}</td>
            <td class="px-4 py-2 text-sm text-red-600 text-center">{cp}</td>
            <td class="px-4 py-2 text-sm text-green-600 text-center">{wp}</td>
            <td class="px-4 py-2 text-right">
                <span id="add-result-{item.product_id}">
                    <button hx-post="/api/shopping-list/items/add-product"
                            hx-vals='{{"product_id": {item.product_id}}}'
                            hx-target="#add-result-{item.product_id}"
                            hx-swap="innerHTML"
                            class="px-2 py-1 bg-blue-100 text-blue-700 text-xs rounded hover:bg-blue-200">
                        Add to list
                    </button>
                </span>
            </td>
        </tr>"""

    html = f"""
    <div class="bg-gray-50 rounded-lg my-2 overflow-hidden border border-gray-200">
        <table class="min-w-full">
            <thead>
                <tr class="bg-gray-100 text-xs font-medium text-gray-500 uppercase">
                    <th class="px-4 py-2 text-left">Product</th>
                    <th class="px-4 py-2 text-center">Qty</th>
                    <th class="px-4 py-2 text-center">Coles</th>
                    <th class="px-4 py-2 text-center">Woolworths</th>
                    <th class="px-4 py-2"></th>
                </tr>
            </thead>
            <tbody class="divide-y divide-gray-200">{rows}</tbody>
        </table>
    </div>"""
    return HTMLResponse(html)


@router.post("/copy/{source_list_id}")
async def copy_list(source_list_id: int, session: AsyncSession = Depends(get_session)):
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
        match = (await session.execute(
            select(ProductMatch).where(
                (ProductMatch.product_a_id == src.product_id) | (ProductMatch.product_b_id == src.product_id),
                ProductMatch.is_rejected == False,  # noqa: E712
            )
        )).scalars().first()

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
    from .views import _shopping_list_context
    from ..templating import templates
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.delete("/purge")
async def purge_shopping_lists(session: AsyncSession = Depends(get_session)):
    items = await session.execute(delete(ShoppingListItem))
    lists = await session.execute(delete(ShoppingList))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {lists.rowcount} lists and {items.rowcount} items.</span>'
    )
