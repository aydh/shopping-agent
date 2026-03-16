from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import ListStatus, Product, ProductMatch, ShoppingList, ShoppingListItem, Store
from ..services.price_comparison import build_price_map
from ..services.shopping_list import (
    choose_best_store,
    confirm_list,
    generate_shopping_list,
    get_active_list,
    get_shopping_list_context as _shopping_list_context,
    remove_item,
    update_item_quantity,
    update_item_store,
)
from ..templating import templates

router = APIRouter()


def _list_header_oob(shopping_list: ShoppingList | None) -> str:
    """Render the OOB list-header fragment."""
    has_list = shopping_list is not None
    return templates.get_template("_list_header.html").render(
        has_list=has_list,
        title=(shopping_list.name if has_list else None) or "Shopping List",
        new_cls="bg-gray-200 text-gray-400 cursor-not-allowed" if has_list else "bg-blue-600 text-white hover:bg-blue-700",
        pred_cls="bg-gray-200 text-gray-400 cursor-not-allowed" if not has_list else "bg-green-600 text-white hover:bg-green-700",
        new_disabled="disabled" if has_list else "",
        pred_disabled="disabled" if not has_list else "",
    )


@router.delete("/current")
async def delete_current_list(session: AsyncSession = Depends(get_session)) -> HTMLResponse:
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
async def new_list(session: AsyncSession = Depends(get_session)) -> HTMLResponse:
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
async def add_predictions(session: AsyncSession = Depends(get_session)) -> HTMLResponse:
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
            chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
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
async def generate(session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    await generate_shopping_list(session)
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


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
    await update_item_store(session, item_id, Store(store))
    ctx = await _shopping_list_context(session)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.post("/set-store/{store}")
async def set_all_store(store: str, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
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


@router.post("/confirm/{list_id}")
async def confirm(list_id: int, session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    await confirm_list(session, list_id)
    return RedirectResponse("/confirm", status_code=303)


@router.post("/close/{list_id}")
async def close_list(list_id: int, session: AsyncSession = Depends(get_session)) -> RedirectResponse:
    """Mark the shopping list as ordered (closed)."""
    shopping_list = await session.get(ShoppingList, list_id)
    if shopping_list:
        shopping_list.status = ListStatus.ORDERED
        await session.commit()
    return RedirectResponse("/shopping-list", status_code=303)


@router.post("/submit-store/{store}")
async def submit_store(store: str, session: AsyncSession = Depends(get_session)) -> RedirectResponse:
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
async def submit_split(session: AsyncSession = Depends(get_session)) -> RedirectResponse:
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
        item.chosen_store = choose_best_store(
            item.coles_price, item.woolworths_price, item.chosen_store or Store.COLES
        )
    shopping_list.status = ListStatus.CONFIRMED
    await session.commit()
    return RedirectResponse("/confirm", status_code=303)


@router.get("/details/{list_id}")
async def list_details(list_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Return an HTML fragment listing all items in a past shopping list."""
    items_result = await session.execute(
        select(ShoppingListItem)
        .where(
            ShoppingListItem.shopping_list_id == list_id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )
    items = items_result.scalars().all()
    product_ids = [i.product_id for i in items]

    products_result = await session.execute(
        select(Product).where(Product.id.in_(product_ids))
    )
    products_by_id = {p.id: p for p in products_result.scalars().all()}

    items_data = [
        {
            "name": products_by_id[i.product_id].name,
            "quantity": i.quantity,
            "coles_price": i.coles_price,
            "woolworths_price": i.woolworths_price,
            "product_id": i.product_id,
        }
        for i in items
        if i.product_id in products_by_id
    ]
    html = templates.get_template("_past_list_details.html").render(items=items_data)
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
        match = (await session.execute(
            select(ProductMatch).where(
                (ProductMatch.product_a_id == src.product_id) | (ProductMatch.product_b_id == src.product_id),
                ProductMatch.is_rejected == False,  # noqa: E712
            )
        )).scalars().first()

        coles_price = None
        woolworths_price = None
        if match:
            pa = await session.get(Product, match.product_a_id)
            pb = await session.get(Product, match.product_b_id)
            coles_p = pa if pa.store == Store.COLES else pb
            ww_p = pa if pa.store == Store.WOOLWORTHS else pb
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


@router.delete("/purge")
async def purge_shopping_lists(session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    items = await session.execute(delete(ShoppingListItem))
    lists = await session.execute(delete(ShoppingList))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {lists.rowcount} lists and {items.rowcount} items.</span>'
    )
