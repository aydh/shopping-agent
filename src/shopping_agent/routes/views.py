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
    product_count = (await session.execute(select(func.count(Product.id)))).scalar() or 0
    order_count = (await session.execute(select(func.count(Order.id)))).scalar() or 0

    today = date.today()
    week_ahead = today + timedelta(days=7)
    runout_query = (
        select(ConsumptionPrediction)
        .options(selectinload(ConsumptionPrediction.product))
        .where(ConsumptionPrediction.predicted_runout_date <= week_ahead)
        .where(ConsumptionPrediction.confidence_score >= 0.3)
        .order_by(ConsumptionPrediction.predicted_runout_date)
    )
    runout_result = await session.execute(runout_query)
    upcoming_runouts = []
    for pred in runout_result.scalars().all():
        pred.days_until_runout = (pred.predicted_runout_date - today).days
        upcoming_runouts.append(pred)

    coles_connected = await coles_scraper.is_authenticated()
    woolworths_connected = await woolworths_scraper.is_authenticated()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_page": "dashboard",
            "product_count": product_count,
            "order_count": order_count,
            "runout_count": len(upcoming_runouts),
            "upcoming_runouts": upcoming_runouts,
            "coles_connected": coles_connected,
            "woolworths_connected": woolworths_connected,
            "coles_last_sync": None,
            "woolworths_last_sync": None,
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


@router.get("/predictions")
async def predictions_page(request: Request, session: AsyncSession = Depends(get_session)):
    today = date.today()
    query = (
        select(ConsumptionPrediction)
        .options(selectinload(ConsumptionPrediction.product))
        .order_by(ConsumptionPrediction.predicted_runout_date)
    )
    result = await session.execute(query)
    predictions = []
    for pred in result.scalars().all():
        pred.days_until_runout = (pred.predicted_runout_date - today).days
        predictions.append(pred)

    return templates.TemplateResponse(
        "predictions.html",
        {
            "request": request,
            "active_page": "predictions",
            "predictions": predictions,
        },
    )


@router.get("/shopping-list")
async def shopping_list_page(request: Request, session: AsyncSession = Depends(get_session)):
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
    if shopping_list:
        for item in shopping_list.items:
            if item.is_removed:
                continue
            cp = (item.coles_price or 0) * item.quantity
            wp = (item.woolworths_price or 0) * item.quantity
            coles_total += cp
            woolworths_total += wp
            best_total += min(cp, wp) if cp and wp else (cp or wp)

    recommendation = ""
    if coles_total and woolworths_total:
        if coles_total < woolworths_total:
            recommendation = f"Coles is ${woolworths_total - coles_total:.2f} cheaper overall"
        elif woolworths_total < coles_total:
            recommendation = f"Woolworths is ${coles_total - woolworths_total:.2f} cheaper overall"
        else:
            recommendation = "Same price at both stores"

    return templates.TemplateResponse(
        "shopping_list.html",
        {
            "request": request,
            "active_page": "shopping_list",
            "shopping_list": shopping_list,
            "coles_total": coles_total,
            "woolworths_total": woolworths_total,
            "best_total": best_total,
            "recommendation": recommendation,
        },
    )


@router.get("/prices")
async def prices_page(request: Request, session: AsyncSession = Depends(get_session)):
    from sqlalchemy.orm import selectinload as sil

    # Fetch all products
    result = await session.execute(
        select(Product)
        .order_by(Product.store, Product.name)
    )
    all_products = list(result.scalars().all())

    # Fetch matches for comparison display
    match_result = await session.execute(
        select(ProductMatch)
        .options(sil(ProductMatch.product_a), sil(ProductMatch.product_b))
        .order_by(ProductMatch.confidence.desc())
    )
    matches = match_result.scalars().all()
    comparisons = _matches_to_comparisons(matches)

    # Determine which products are already matched
    matched_ids = set()
    for m in matches:
        matched_ids.add(m.product_a_id)
        matched_ids.add(m.product_b_id)

    unmatched_coles = [p for p in all_products if p.store == Store.COLES and p.id not in matched_ids]
    unmatched_woolworths = [p for p in all_products if p.store == Store.WOOLWORTHS and p.id not in matched_ids]

    return templates.TemplateResponse(
        "prices.html",
        {
            "request": request,
            "active_page": "prices",
            "comparisons": comparisons,
            "all_products": all_products,
            "unmatched_coles": unmatched_coles,
            "unmatched_woolworths": unmatched_woolworths,
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

    if shopping_list:
        for item in shopping_list.items:
            if item.is_removed:
                continue
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
            <td class="px-6 py-3 text-sm font-medium text-gray-900">{label}</td>
            <td class="px-6 py-3 text-sm text-gray-500" id="{tid}-count">{count}</td>
            <td class="px-6 py-3 text-xs text-gray-400">{also}</td>
            <td class="px-6 py-3 text-right">
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
