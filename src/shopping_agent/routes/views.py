from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..database import get_session
from ..models import (
    ConsumptionPrediction,
    ListStatus,
    Order,
    Product,
    ShoppingList,
    ShoppingListItem,
    Store,
)
from ..scrapers.browser_manager import browser_manager
from ..templating import templates

router = APIRouter()


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

    coles_connected = await browser_manager.is_authenticated(Store.COLES)
    woolworths_connected = await browser_manager.is_authenticated(Store.WOOLWORTHS)

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
    return templates.TemplateResponse(
        "prices.html",
        {
            "request": request,
            "active_page": "prices",
            "comparisons": [],
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


@router.get("/settings")
async def settings_page(request: Request):
    coles_connected = await browser_manager.is_authenticated(Store.COLES)
    woolworths_connected = await browser_manager.is_authenticated(Store.WOOLWORTHS)

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_page": "settings",
            "coles_connected": coles_connected,
            "woolworths_connected": woolworths_connected,
        },
    )
