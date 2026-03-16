"""Dashboard page view."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import MIN_PREDICTION_CONFIDENCE
from ...database import get_session
from ...models import (
    ConsumptionPrediction,
    Order,
    Product,
    ProductMatch,
    ShoppingList,
    Store,
)
from ...services.prediction import get_predictions_with_match_info
from ...services.shopping_list import get_shopping_list_context
from ...templating import templates

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Render the dashboard page."""
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

    upcoming_runouts_all = await get_predictions_with_match_info(session, max_runout_date=week_ahead)
    upcoming_runouts = [
        p for p in upcoming_runouts_all
        if p.confidence_score >= MIN_PREDICTION_CONFIDENCE
    ]

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
    sl_ctx = await get_shopping_list_context(session)

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
