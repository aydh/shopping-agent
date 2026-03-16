from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..database import get_session
from ..models import (
    ConsumptionPrediction,
    ListStatus,
    Order,
    OrderItem,
    Product,
    ProductMatch,
    ShoppingList,
    ShoppingListItem,
    Store,
)
from ..services.price_comparison import PriceComparison
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper
from ..templating import templates

router = APIRouter()


def _matches_to_comparisons(matches: list) -> list[PriceComparison]:
    """Convert ProductMatch rows into PriceComparison dataclasses."""
    comparisons = []
    for match in matches:
        pa, pb = match.product_a, match.product_b
        coles_p = pa if pa.store == Store.COLES else pb
        ww_p = pa if pa.store == Store.WOOLWORTHS else pb

        cp = coles_p.current_price
        wp = ww_p.current_price
        cheaper = None
        savings = 0.0
        if cp and wp:
            if cp < wp:
                cheaper = Store.COLES
                savings = wp - cp
            elif wp < cp:
                cheaper = Store.WOOLWORTHS
                savings = cp - wp

        comparisons.append(PriceComparison(
            product_name=coles_p.name,
            unit_size=coles_p.unit_size,
            product_id=coles_p.id,
            coles_product=coles_p,
            woolworths_product=ww_p,
            coles_price=cp,
            woolworths_price=wp,
            cheaper_store=cheaper,
            savings=savings,
            match_id=match.id,
            match_confidence=match.confidence,
            is_confirmed=match.is_confirmed,
            match_method=match.match_method,
        ))
    return comparisons


@router.get("/")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    today = date.today()
    week_ahead = today + timedelta(days=7)

    # Orders per store
    coles_orders = (await session.execute(select(func.count(Order.id)).where(Order.store == Store.COLES))).scalar() or 0
    ww_orders = (await session.execute(select(func.count(Order.id)).where(Order.store == Store.WOOLWORTHS))).scalar() or 0

    # Products per store
    coles_products = (await session.execute(select(func.count(Product.id)).where(Product.store == Store.COLES))).scalar() or 0
    ww_products = (await session.execute(select(func.count(Product.id)).where(Product.store == Store.WOOLWORTHS))).scalar() or 0

    # Removed (hidden) products
    removed_count = (await session.execute(
        select(func.count(Product.id)).where(Product.is_hidden == True)  # noqa: E712
    )).scalar() or 0

    # Matches
    matched_count = (await session.execute(
        select(func.count(ProductMatch.id)).where(ProductMatch.is_rejected == False)  # noqa: E712
    )).scalar() or 0
    rejected_count = (await session.execute(
        select(func.count(ProductMatch.id)).where(ProductMatch.is_rejected == True)  # noqa: E712
    )).scalar() or 0

    # Predictions + running low
    pred_count = (await session.execute(select(func.count(ConsumptionPrediction.id)))).scalar() or 0
    runout_result = await session.execute(
        select(ConsumptionPrediction)
        .options(selectinload(ConsumptionPrediction.product))
        .where(ConsumptionPrediction.predicted_runout_date <= week_ahead)
        .where(ConsumptionPrediction.confidence_score >= 0.3)
        .order_by(ConsumptionPrediction.predicted_runout_date)
    )
    matches_result = await session.execute(
        select(ProductMatch)
        .where(ProductMatch.is_rejected == False)  # noqa: E712
        .options(
            selectinload(ProductMatch.product_a),
            selectinload(ProductMatch.product_b),
        )
    )
    matched_product_map: dict[int, Product] = {}
    match_id_map_runout: dict[int, int] = {}
    for m in matches_result.scalars().all():
        matched_product_map[m.product_a_id] = m.product_b
        matched_product_map[m.product_b_id] = m.product_a
        match_id_map_runout[m.product_a_id] = m.id
        match_id_map_runout[m.product_b_id] = m.id
    upcoming_runouts = []
    for pred in runout_result.scalars().all():
        pred.days_until_runout = (pred.predicted_runout_date - today).days
        other = matched_product_map.get(pred.product_id)
        pred.is_matched = other is not None
        pred.matched_product = other
        pred.match_id = match_id_map_runout.get(pred.product_id)
        upcoming_runouts.append(pred)

    # Shopping lists
    list_count = (await session.execute(select(func.count(ShoppingList.id)))).scalar() or 0

    # Last sync per store (latest order date)
    coles_last_sync = (await session.execute(
        select(func.max(Order.order_date)).where(Order.store == Store.COLES)
    )).scalar()
    ww_last_sync = (await session.execute(
        select(func.max(Order.order_date)).where(Order.store == Store.WOOLWORTHS)
    )).scalar()

    # Current shopping list context
    sl_ctx = await _shopping_list_context(session)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_page": "dashboard",
            "coles_orders": coles_orders,
            "ww_orders": ww_orders,
            "coles_products": coles_products,
            "ww_products": ww_products,
            "removed_count": removed_count,
            "matched_count": matched_count,
            "rejected_count": rejected_count,
            "pred_count": pred_count,
            "runout_count": len(upcoming_runouts),
            "upcoming_runouts": upcoming_runouts,
            "list_count": list_count,
            "coles_last_sync": coles_last_sync,
            "ww_last_sync": ww_last_sync,
            **sl_ctx,
        },
    )


@router.get("/orders")
async def orders_page(
    request: Request,
    store: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    query = select(Order).options(selectinload(Order.items)).order_by(Order.order_date.desc())
    if store in ("coles", "woolworths"):
        query = query.where(Order.store == Store(store))
    result = await session.execute(query)
    orders = result.scalars().all()

    return templates.TemplateResponse(
        "orders.html",
        {
            "request": request,
            "active_page": "orders",
            "orders": orders,
            "store_filter": store or "all",
        },
    )


async def _predictions_list(session: AsyncSession) -> list:
    today = date.today()
    result = await session.execute(
        select(ConsumptionPrediction)
        .options(selectinload(ConsumptionPrediction.product))
        .order_by(ConsumptionPrediction.predicted_runout_date)
    )

    # Load non-rejected matches with both products eager-loaded so the template
    # can show both store products when a prediction covers a matched pair.
    matches_result = await session.execute(
        select(ProductMatch)
        .where(ProductMatch.is_rejected == False)  # noqa: E712
        .options(
            selectinload(ProductMatch.product_a),
            selectinload(ProductMatch.product_b),
        )
    )
    # Map each product_id to its matched counterpart and match id
    matched_product: dict[int, Product] = {}
    match_id_map: dict[int, int] = {}
    for m in matches_result.scalars().all():
        matched_product[m.product_a_id] = m.product_b
        matched_product[m.product_b_id] = m.product_a
        match_id_map[m.product_a_id] = m.id
        match_id_map[m.product_b_id] = m.id

    predictions = []
    for pred in result.scalars().all():
        pred.days_until_runout = (pred.predicted_runout_date - today).days
        other = matched_product.get(pred.product_id)
        pred.is_matched = other is not None
        pred.matched_product = other
        pred.match_id = match_id_map.get(pred.product_id)
        predictions.append(pred)
    return predictions


@router.get("/predictions")
async def predictions_page(request: Request, session: AsyncSession = Depends(get_session)):
    predictions = await _predictions_list(session)
    return templates.TemplateResponse(
        "predictions.html",
        {"request": request, "active_page": "predictions", "predictions": predictions},
    )


async def _resolve_display_names(
    session: AsyncSession, items: list
) -> tuple[dict[int, str], dict[int, dict], dict[int, dict]]:
    """Return (display_names, store_names, store_products) dicts.
    display_names: {item_id: name} using chosen store's product name.
    store_names: {item_id: {'coles': name|None, 'woolworths': name|None}}
    store_products: {item_id: {'coles': Product|None, 'woolworths': Product|None}}
    """
    display_names: dict[int, str] = {}
    store_names: dict[int, dict] = {}
    store_products: dict[int, dict] = {}
    for item in items:
        if item.is_removed:
            continue
        canonical = item.product
        partner = None
        match_result = await session.execute(
            select(ProductMatch).where(
                (
                    (ProductMatch.product_a_id == canonical.id)
                    | (ProductMatch.product_b_id == canonical.id)
                ),
                ProductMatch.is_rejected == False,  # noqa: E712
            )
        )
        match = match_result.scalars().first()
        if match:
            partner_id = (
                match.product_b_id if match.product_a_id == canonical.id else match.product_a_id
            )
            partner = await session.get(Product, partner_id)

        if canonical.store == Store.COLES:
            coles_product = canonical
            ww_product = partner if partner and partner.store == Store.WOOLWORTHS else None
        else:
            ww_product = canonical
            coles_product = partner if partner and partner.store == Store.COLES else None

        coles_name = coles_product.name if coles_product else None
        woolworths_name = ww_product.name if ww_product else None

        store_names[item.id] = {"coles": coles_name, "woolworths": woolworths_name}
        store_products[item.id] = {"coles": coles_product, "woolworths": ww_product}

        if item.chosen_store == Store.COLES and coles_name:
            display_names[item.id] = coles_name
        elif item.chosen_store == Store.WOOLWORTHS and woolworths_name:
            display_names[item.id] = woolworths_name
        else:
            display_names[item.id] = canonical.name

    return display_names, store_names, store_products


async def _shopping_list_context(session: AsyncSession) -> dict:
    query = (
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    result = await session.execute(query)
    shopping_list = result.scalars().first()

    coles_total = 0.0
    woolworths_total = 0.0
    best_total = 0.0
    display_names: dict[int, str] = {}
    store_names: dict[int, dict] = {}
    store_products: dict[int, dict] = {}
    single_store: Store | None = None
    if shopping_list:
        display_names, store_names, store_products = await _resolve_display_names(session, shopping_list.items)
        active_items = [i for i in shopping_list.items if not i.is_removed]
        stores_used = {i.chosen_store for i in active_items if i.chosen_store}
        single_store = stores_used.pop() if len(stores_used) == 1 else None
        for item in active_items:
            cp = item.coles_price * item.quantity if item.coles_price else None
            wp = item.woolworths_price * item.quantity if item.woolworths_price else None
            # Each store total uses that store's price where available, falls back to
            # the other store so all three totals always represent the full basket.
            coles_total += cp if cp is not None else (wp or 0)
            woolworths_total += wp if wp is not None else (cp or 0)
            best_total += min(cp, wp) if cp is not None and wp is not None else (cp or wp or 0)

    recommendation = ""
    if coles_total and woolworths_total:
        if coles_total < woolworths_total:
            recommendation = f"Coles is ${woolworths_total - coles_total:.2f} cheaper overall"
        elif woolworths_total < coles_total:
            recommendation = f"Woolworths is ${coles_total - woolworths_total:.2f} cheaper overall"
        else:
            recommendation = "Same price at both stores"

    return {
        "shopping_list": shopping_list,
        "display_names": display_names,
        "store_names": store_names,
        "store_products": store_products,
        "single_store": single_store,
        "coles_total": coles_total,
        "woolworths_total": woolworths_total,
        "best_total": best_total,
        "recommendation": recommendation,
    }


async def _shopping_list_history(session: AsyncSession) -> list[dict]:
    """Return summary rows for past (ordered/confirmed) shopping lists."""
    result = await session.execute(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items))
        .where(ShoppingList.status == ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    rows = []
    for sl in result.scalars().all():
        active = [i for i in sl.items if not i.is_removed]
        stores = {i.chosen_store for i in active if i.chosen_store}
        store = stores.pop() if len(stores) == 1 else None
        total = 0.0
        for i in active:
            price = (i.coles_price if store == Store.COLES else i.woolworths_price) or 0
            total += price * i.quantity
        rows.append({
            "id": sl.id,
            "name": sl.name,
            "created_at": sl.created_at,
            "status": sl.status,
            "store": store,
            "item_count": len(active),
            "total": total,
        })
    return rows


@router.get("/shopping-list")
async def shopping_list_page(request: Request, session: AsyncSession = Depends(get_session)):
    ctx = await _shopping_list_context(session)
    past_lists = await _shopping_list_history(session)
    return templates.TemplateResponse(
        "shopping_list.html",
        {"request": request, "active_page": "shopping_list", "past_lists": past_lists, **ctx},
    )


@router.get("/prices")
async def prices_page(request: Request, session: AsyncSession = Depends(get_session)):
    from sqlalchemy.orm import selectinload as sil

    # Fetch all visible products
    result = await session.execute(
        select(Product)
        .where(Product.is_hidden == False)  # noqa: E712
        .order_by(Product.store, Product.name)
    )
    all_products = list(result.scalars().all())
    visible_ids = {p.id for p in all_products}

    # Fetch active matches (exclude rejected, exclude matches where either product is hidden)
    match_result = await session.execute(
        select(ProductMatch)
        .options(sil(ProductMatch.product_a), sil(ProductMatch.product_b))
        .where(ProductMatch.is_rejected == False)  # noqa: E712
        .order_by(ProductMatch.confidence.desc())
    )
    matches = [m for m in match_result.scalars().all()
               if m.product_a_id in visible_ids and m.product_b_id in visible_ids]
    comparisons = _matches_to_comparisons(matches)

    matched_ids = set()
    for m in matches:
        matched_ids.add(m.product_a_id)
        matched_ids.add(m.product_b_id)

    unmatched_coles = [p for p in all_products if p.store == Store.COLES and p.id not in matched_ids]
    unmatched_woolworths = [p for p in all_products if p.store == Store.WOOLWORTHS and p.id not in matched_ids]

    # Last ordered date for all visible products (single query)
    from sqlalchemy import func as sqlfunc
    lo_rows = await session.execute(
        select(OrderItem.product_id, sqlfunc.max(Order.order_date))
        .join(Order, OrderItem.order_id == Order.id)
        .where(OrderItem.product_id.in_(visible_ids))
        .group_by(OrderItem.product_id)
    )
    last_ordered: dict[int, date] = dict(lo_rows.all())

    # Fetch rejected matches
    rejected_result = await session.execute(
        select(ProductMatch)
        .options(sil(ProductMatch.product_a), sil(ProductMatch.product_b))
        .where(ProductMatch.is_rejected == True)  # noqa: E712
        .order_by(ProductMatch.updated_at.desc())
    )
    rejected_matches = rejected_result.scalars().all()

    # Fetch hidden products with order history for last ordered date
    hidden_result = await session.execute(
        select(Product)
        .options(sil(Product.order_items).selectinload(OrderItem.order))
        .where(Product.is_hidden == True)  # noqa: E712
        .order_by(Product.store, Product.name)
    )
    hidden_products_raw = hidden_result.scalars().all()
    hidden_products = []
    for p in hidden_products_raw:
        dates = [oi.order.order_date for oi in p.order_items if oi.order]
        p.last_ordered_date = max(dates) if dates else None
        hidden_products.append(p)

    # Fetch unavailable products (is_available=False, not hidden)
    unavailable_result = await session.execute(
        select(Product)
        .where(Product.is_available == False)  # noqa: E712
        .where(Product.is_hidden == False)  # noqa: E712
        .order_by(Product.store, Product.name)
    )
    unavailable_products = list(unavailable_result.scalars().all())

    return templates.TemplateResponse(
        "prices.html",
        {
            "request": request,
            "active_page": "prices",
            "comparisons": comparisons,
            "unmatched_coles": unmatched_coles,
            "unmatched_woolworths": unmatched_woolworths,
            "rejected_matches": rejected_matches,
            "hidden_products": hidden_products,
            "unavailable_products": unavailable_products,
            "last_ordered": last_ordered,
        },
    )


@router.get("/confirm")
async def confirm_page(request: Request, session: AsyncSession = Depends(get_session)):
    query = (
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status == ListStatus.CONFIRMED)
        .order_by(ShoppingList.created_at.desc())
    )
    result = await session.execute(query)
    shopping_list = result.scalars().first()

    coles_items = []
    woolworths_items = []
    coles_total = 0.0
    woolworths_total = 0.0

    display_names: dict[int, str] = {}
    if shopping_list:
        all_items = [i for i in shopping_list.items if not i.is_removed]
        display_names, _, _sp = await _resolve_display_names(session, all_items)
        for item in all_items:
            if item.chosen_store == Store.COLES:
                coles_items.append(item)
                coles_total += (item.coles_price or 0) * item.quantity
            else:
                woolworths_items.append(item)
                woolworths_total += (item.woolworths_price or 0) * item.quantity

    return templates.TemplateResponse(
        "confirm.html",
        {
            "request": request,
            "active_page": "confirm",
            "shopping_list": shopping_list,
            "display_names": display_names,
            "coles_items": coles_items,
            "woolworths_items": woolworths_items,
            "coles_total": coles_total,
            "woolworths_total": woolworths_total,
        },
    )


async def _get_counts(session: AsyncSession) -> dict:
    from ..models import ConsumptionPrediction, PriceHistory, ProductMatch, ShoppingListItem
    return {
        "coles_orders": (await session.execute(select(func.count(Order.id)).where(Order.store == Store.COLES))).scalar(),
        "coles_order_items": (await session.execute(
            select(func.count(OrderItem.id)).join(Order).where(Order.store == Store.COLES)
        )).scalar(),
        "woolworths_orders": (await session.execute(select(func.count(Order.id)).where(Order.store == Store.WOOLWORTHS))).scalar(),
        "woolworths_order_items": (await session.execute(
            select(func.count(OrderItem.id)).join(Order).where(Order.store == Store.WOOLWORTHS)
        )).scalar(),
        "coles_products": (await session.execute(select(func.count(Product.id)).where(Product.store == Store.COLES))).scalar(),
        "woolworths_products": (await session.execute(select(func.count(Product.id)).where(Product.store == Store.WOOLWORTHS))).scalar(),
        "product_matches": (await session.execute(select(func.count(ProductMatch.id)))).scalar(),
        "price_history": (await session.execute(select(func.count(PriceHistory.id)))).scalar(),
        "predictions": (await session.execute(select(func.count(ConsumptionPrediction.id)))).scalar(),
        "shopping_lists": (await session.execute(select(func.count(ShoppingList.id)))).scalar(),
        "shopping_list_items": (await session.execute(select(func.count(ShoppingListItem.id)))).scalar(),
    }


def _counts_rows_html(counts: dict) -> str:
    rows = [
        ("Coles Orders", f"{counts['coles_orders']} orders, {counts['coles_order_items']} items", "price history", "/api/orders/purge/coles", "purge-coles"),
        ("Woolworths Orders", f"{counts['woolworths_orders']} orders, {counts['woolworths_order_items']} items", "price history", "/api/orders/purge/woolworths", "purge-woolworths"),
        ("Coles Products", str(counts["coles_products"]), "matches, price history, predictions", "/api/prices/products/purge/coles", "purge-coles-products"),
        ("Woolworths Products", str(counts["woolworths_products"]), "matches, price history, predictions", "/api/prices/products/purge/woolworths", "purge-woolworths-products"),
        ("Product Matches", str(counts["product_matches"]), "—", "/api/prices/matches/purge", "purge-matches"),
        ("Price History", str(counts["price_history"]), "—", "/api/prices/history/purge", "purge-price-history"),
        ("Predictions", str(counts["predictions"]), "—", "/api/predictions/purge", "purge-predictions"),
        ("Shopping Lists", f"{counts['shopping_lists']} lists, {counts['shopping_list_items']} items", "—", "/api/shopping-list/purge", "purge-lists"),
    ]
    html = ""
    for label, count, also, endpoint, tid in rows:
        html += f"""<tr>
            <td class="px-3 sm:px-6 py-3 text-sm font-medium text-gray-900">{label}</td>
            <td class="px-3 sm:px-6 py-3 text-sm text-gray-500" id="{tid}-count">{count}</td>
            <td class="px-3 sm:px-6 py-3 text-xs text-gray-400 hidden sm:table-cell">{also}</td>
            <td class="px-3 sm:px-6 py-3 text-right">
                <span id="{tid}-result" class="mr-2 text-sm"></span>
                <button
                    hx-delete="{endpoint}"
                    hx-target="#{tid}-result"
                    hx-on:htmx:after-request="htmx.trigger('#data-mgmt-body', 'countsRefresh')"
                    class="px-3 py-1.5 bg-orange-100 text-orange-700 text-xs rounded hover:bg-orange-200">
                    Purge
                </button>
            </td>
        </tr>"""
    return html


@router.get("/api/settings/counts")
async def settings_counts(session: AsyncSession = Depends(get_session)):
    counts = await _get_counts(session)
    return HTMLResponse(_counts_rows_html(counts))


@router.get("/settings")
async def settings_page(request: Request, session: AsyncSession = Depends(get_session)):
    coles_connected = await coles_scraper.is_authenticated()
    woolworths_connected = await woolworths_scraper.is_authenticated()
    counts = await _get_counts(session)

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_page": "settings",
            "coles_connected": coles_connected,
            "woolworths_connected": woolworths_connected,
            "counts": counts,
            "counts_rows_html": _counts_rows_html(counts),
        },
    )
