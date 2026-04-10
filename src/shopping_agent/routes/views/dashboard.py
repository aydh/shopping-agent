"""Dashboard page view."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ...auth import CurrentUser, get_current_user_from_cookie
from ...config import MIN_PREDICTION_CONFIDENCE
from ...database import get_user_session_from_cookie
from ...models import (
    ConsumptionPrediction,
    Order,
    OrderItem,
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

    # Orders per store: count, total spend, last order date — single grouped query
    order_stats = {
        row.store: row for row in (await session.execute(
            select(
                Order.store,
                func.count(Order.id).label("count"),
                func.max(Order.order_date).label("last_date"),
                func.sum(Order.total_amount).label("total_spend"),
            )
            .group_by(Order.store)
        )).all()
    }
    coles_orders = order_stats.get(Store.COLES, (None, 0, None, None)).count if Store.COLES in order_stats else 0
    ww_orders = order_stats.get(Store.WOOLWORTHS, (None, 0, None, None)).count if Store.WOOLWORTHS in order_stats else 0
    coles_last_sync = order_stats[Store.COLES].last_date if Store.COLES in order_stats else None
    ww_last_sync = order_stats[Store.WOOLWORTHS].last_date if Store.WOOLWORTHS in order_stats else None

    # Latest order per store (DISTINCT ON): last spend + item count
    latest_orders_sq = (
        select(Order.id.label("order_id"), Order.store, Order.total_amount.label("last_spend"))
        .distinct(Order.store)
        .order_by(Order.store, Order.order_date.desc(), Order.id.desc())
        .subquery()
    )
    last_order_stats = {
        row.store: row for row in (await session.execute(
            select(
                latest_orders_sq.c.store,
                latest_orders_sq.c.last_spend,
                func.count(OrderItem.id).label("item_count"),
            )
            .outerjoin(OrderItem, OrderItem.order_id == latest_orders_sq.c.order_id)
            .group_by(latest_orders_sq.c.store, latest_orders_sq.c.last_spend)
        )).all()
    }

    # Products per store — comprehensive stats (all products, including hidden)
    product_detail_stats: dict = {}
    for row in (await session.execute(
        select(
            Product.store,
            func.count(Product.id).label("total"),
            func.count(Product.id).filter(Product.is_available.is_(False)).label("unavailable"),
            func.count(Product.id).filter(Product.not_found.is_(True)).label("not_found"),
            func.max(Product.updated_at).label("last_updated"),
        )
        .group_by(Product.store)
    )).all():
        product_detail_stats[row.store] = row

    coles_pstats = product_detail_stats.get(Store.COLES)
    ww_pstats = product_detail_stats.get(Store.WOOLWORTHS)
    coles_products = coles_pstats.total if coles_pstats else 0
    ww_products = ww_pstats.total if ww_pstats else 0

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

    # Subquery: product IDs that have a non-rejected match on either side
    matched_ids_sq = (
        select(ProductMatch.product_a_id.label("pid")).where(ProductMatch.is_rejected.is_(False))
        .union(select(ProductMatch.product_b_id).where(ProductMatch.is_rejected.is_(False)))
        .subquery()
    )

    # Prediction stats per store: total + how many have a cross-store match
    pred_store_stats: dict = {}
    for pred_row in (await session.execute(
        select(
            Product.store,
            func.count(ConsumptionPrediction.id).label("total"),
            func.count(ConsumptionPrediction.id).filter(
                matched_ids_sq.c.pid.isnot(None)
            ).label("matched"),
        )
        .join(Product, Product.id == ConsumptionPrediction.product_id)
        .outerjoin(matched_ids_sq, matched_ids_sq.c.pid == Product.id)
        .group_by(Product.store)
    )).all():
        pred_store_stats[pred_row.store] = pred_row

    # Inactive stats per store: unavailable + not_found, each with matched count
    inactive_store_stats: dict = {}
    for inactive_row in (await session.execute(
        select(
            Product.store,
            func.count(Product.id).filter(Product.is_available.is_(False)).label("unavailable"),
            func.count(Product.id).filter(
                and_(Product.is_available.is_(False), matched_ids_sq.c.pid.isnot(None))
            ).label("unavailable_matched"),
            func.count(Product.id).filter(Product.not_found.is_(True)).label("not_found"),
            func.count(Product.id).filter(
                and_(Product.not_found.is_(True), matched_ids_sq.c.pid.isnot(None))
            ).label("not_found_matched"),
        )
        .outerjoin(matched_ids_sq, matched_ids_sq.c.pid == Product.id)
        .group_by(Product.store)
    )).all():
        inactive_store_stats[inactive_row.store] = inactive_row

    pred_count = sum(r.total for r in pred_store_stats.values())

    # Pair-level matched stats: both sides unavailable / both not found
    _ProductA = aliased(Product)
    _ProductB = aliased(Product)
    _pair_stats = (await session.execute(
        select(
            func.count(ProductMatch.id).filter(
                and_(_ProductA.is_available.is_(False), _ProductB.is_available.is_(False))
            ).label("both_unavailable"),
            func.count(ProductMatch.id).filter(
                and_(_ProductA.not_found.is_(True), _ProductB.not_found.is_(True))
            ).label("both_not_found"),
        )
        .select_from(ProductMatch)
        .join(_ProductA, _ProductA.id == ProductMatch.product_a_id)
        .join(_ProductB, _ProductB.id == ProductMatch.product_b_id)
        .where(ProductMatch.is_rejected.is_(False))
    )).one()
    matched_unavailable = _pair_stats.both_unavailable
    matched_not_found = _pair_stats.both_not_found

    # List count
    list_count = (await session.execute(select(func.count(ShoppingList.id)))).scalar() or 0

    upcoming_runouts_all = await get_predictions_with_match_info(session, user.user_id, max_runout_date=week_ahead)
    upcoming_runouts = [
        p for p in upcoming_runouts_all
        if p.confidence_score >= MIN_PREDICTION_CONFIDENCE
    ]

    # Bucket prediction totals into: coles-only, matched, ww-only
    coles_pred_row = pred_store_stats.get(Store.COLES)
    ww_pred_row = pred_store_stats.get(Store.WOOLWORTHS)
    coles_matched_n = coles_pred_row.matched if coles_pred_row else 0
    ww_matched_n = ww_pred_row.matched if ww_pred_row else 0
    pred_totals = {
        "coles":   (coles_pred_row.total if coles_pred_row else 0) - coles_matched_n,
        "matched": coles_matched_n + ww_matched_n,
        "ww":      (ww_pred_row.total if ww_pred_row else 0) - ww_matched_n,
    }

    # Bucket running-low predictions the same way, split by availability
    def _inactive(product) -> bool:
        return not product.is_available or product.not_found

    runout_buckets: dict = {
        k: {"total": 0, "available": 0, "inactive": 0}
        for k in ("coles", "matched", "ww")
    }
    for p in upcoming_runouts:
        if p.is_matched:
            runout_buckets["matched"]["total"] += 1
            # available = at least one store can fulfil; inactive = both stores gone
            if _inactive(p.product) and _inactive(p.matched_product):
                runout_buckets["matched"]["inactive"] += 1
            else:
                runout_buckets["matched"]["available"] += 1
        else:
            bucket = "coles" if p.product.store == Store.COLES else "ww"
            runout_buckets[bucket]["total"] += 1
            if _inactive(p.product):
                runout_buckets[bucket]["inactive"] += 1
            else:
                runout_buckets[bucket]["available"] += 1

    # Current shopping list context
    sl_ctx = await get_shopping_list_context(session, user.user_id)

    # Shopping list tile: per-bucket product counts + priority/split prices
    sl_buckets: dict = {
        k: {"total": 0, "unavailable": 0} for k in ("coles", "matched", "ww")
    }
    sl_prices = {"coles_primary": 0.0, "coles_topup": 0.0, "ww_primary": 0.0, "ww_topup": 0.0}

    sl_list = sl_ctx.get("shopping_list")
    if sl_list:
        sl_store_products = sl_ctx.get("store_products", {})
        for item in (i for i in sl_list.items if not i.is_removed):
            spm = sl_store_products.get(item.id, {})
            cp_prod = spm.get("coles")
            wp_prod = spm.get("woolworths")
            coles_avail = cp_prod is not None and cp_prod.is_available
            ww_avail = wp_prod is not None and wp_prod.is_available

            if cp_prod and wp_prod:
                sl_buckets["matched"]["total"] += 1
                if not coles_avail and not ww_avail:
                    sl_buckets["matched"]["unavailable"] += 1
            elif cp_prod:
                sl_buckets["coles"]["total"] += 1
                if not coles_avail:
                    sl_buckets["coles"]["unavailable"] += 1
            elif wp_prod:
                sl_buckets["ww"]["total"] += 1
                if not ww_avail:
                    sl_buckets["ww"]["unavailable"] += 1

            cp_line = (item.coles_price or 0) * item.quantity if coles_avail else None
            wp_line = (item.woolworths_price or 0) * item.quantity if ww_avail else None

            if cp_line is not None:
                sl_prices["coles_primary"] += cp_line
            elif wp_line is not None:
                sl_prices["coles_topup"] += wp_line

            if wp_line is not None:
                sl_prices["ww_primary"] += wp_line
            elif cp_line is not None:
                sl_prices["ww_topup"] += cp_line

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_page": "dashboard",
            "coles_orders": coles_orders,
            "ww_orders": ww_orders,
            "coles_ostats": order_stats.get(Store.COLES),
            "ww_ostats": order_stats.get(Store.WOOLWORTHS),
            "coles_last_order": last_order_stats.get(Store.COLES),
            "ww_last_order": last_order_stats.get(Store.WOOLWORTHS),
            "coles_products": coles_products,
            "ww_products": ww_products,
            "coles_pstats": coles_pstats,
            "ww_pstats": ww_pstats,
            "removed_count": removed_count,
            "matched_count": matched_count,
            "matched_unavailable": matched_unavailable,
            "matched_not_found": matched_not_found,
            "rejected_count": rejected_count,
            "pred_count": pred_count,
            "pred_totals": pred_totals,
            "runout_buckets": runout_buckets,
            "runout_count": len(upcoming_runouts),
            "runout_days": runout_days,
            "upcoming_runouts": upcoming_runouts,
            "list_count": list_count,
            "sl_buckets": sl_buckets,
            "sl_prices": sl_prices,
            "coles_last_sync": coles_last_sync,
            "ww_last_sync": ww_last_sync,
            **sl_ctx,
        },
    )
