"""Dashboard page view."""
import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import CurrentUser, get_current_user_from_cookie
from ...config import MIN_PREDICTION_CONFIDENCE
from ...database import get_user_session_from_cookie
from ...db_helpers import hidden_product_ids_subquery
from ...models import (
    ConsumptionPrediction,
    Order,
    Product,
    ProductMatch,
    ShoppingList,
    Store,
    UserProductPreferences,
)
from ...services.prediction import get_predictions_with_match_info
from ...services.shopping_list import get_shopping_list_context
from ...templating import templates

router = APIRouter()


@router.get("/")
async def dashboard(
    request: Request,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Render the dashboard page."""
    today = date.today()
    runout_days = 7
    week_ahead = today + timedelta(days=runout_days)

    # Orders per store + last sync date — single grouped query
    order_stats = {
        row.store: row for row in (await session.execute(
            select(Order.store, func.count(Order.id), func.max(Order.order_date))
            .group_by(Order.store)
        )).all()
    }
    coles_orders = order_stats.get(Store.COLES, (None, 0, None))[1]
    ww_orders = order_stats.get(Store.WOOLWORTHS, (None, 0, None))[1]
    coles_last_sync = order_stats.get(Store.COLES, (None, 0, None))[2]
    ww_last_sync = order_stats.get(Store.WOOLWORTHS, (None, 0, None))[2]

    # Products per store — single grouped query, excluding this user's hidden products
    hidden_subq = hidden_product_ids_subquery(user.user_id)
    product_stats = {
        row.store: row[1] for row in (await session.execute(
            select(Product.store, func.count(Product.id))
            .where(Product.id.notin_(hidden_subq))
            .group_by(Product.store)
        )).all()
    }
    coles_products = product_stats.get(Store.COLES, 0)
    ww_products = product_stats.get(Store.WOOLWORTHS, 0)

    # Hidden products count (per this user)
    removed_count = (await session.execute(
        select(func.count(UserProductPreferences.product_id))
        .where(
            UserProductPreferences.user_id == user.user_id,
            UserProductPreferences.is_hidden.is_(True),
        )
    )).scalar() or 0

    # Matches — single grouped query
    match_stats = {
        row.is_rejected: row[1] for row in (await session.execute(
            select(ProductMatch.is_rejected, func.count(ProductMatch.id))
            .group_by(ProductMatch.is_rejected)
        )).all()
    }
    matched_count = match_stats.get(False, 0)
    rejected_count = match_stats.get(True, 0)

    # Predictions + running low, list count — run concurrently
    pred_count_task = session.execute(select(func.count(ConsumptionPrediction.id)))
    list_count_task = session.execute(select(func.count(ShoppingList.id)))
    pred_count_result, list_count_result = await asyncio.gather(pred_count_task, list_count_task)
    pred_count = pred_count_result.scalar() or 0
    list_count = list_count_result.scalar() or 0

    upcoming_runouts_all = await get_predictions_with_match_info(session, user.user_id, max_runout_date=week_ahead)
    upcoming_runouts = [
        p for p in upcoming_runouts_all
        if p.confidence_score >= MIN_PREDICTION_CONFIDENCE
    ]

    # Current shopping list context
    sl_ctx = await get_shopping_list_context(session, user.user_id)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
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
            "runout_days": runout_days,
            "upcoming_runouts": upcoming_runouts,
            "list_count": list_count,
            "coles_last_sync": coles_last_sync,
            "ww_last_sync": ww_last_sync,
            **sl_ctx,
        },
    )
