"""Price history chart endpoints."""
import logging
from datetime import date as date_type

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...config import COLES_COLOUR, PRICE_LINE_COLOUR, WOOLWORTHS_COLOUR
from ...database import get_session
from ...models import PriceHistory, Product, ProductMatch, Store
from ...templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/product-history/{product_id}")
async def product_price_history(product_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Return a chart + table of price history for a single product."""
    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse("")

    rows = (await session.execute(
        select(sqlfunc.date(PriceHistory.recorded_at), sqlfunc.avg(PriceHistory.price))
        .where(PriceHistory.product_id == product_id)
        .group_by(sqlfunc.date(PriceHistory.recorded_at))
        .order_by(sqlfunc.date(PriceHistory.recorded_at))
    )).all()

    def fmt(dt_str, f): return (dt_str if isinstance(dt_str, date_type) else date_type.fromisoformat(dt_str)).strftime(f)

    is_coles = product.store == Store.COLES
    label = "Coles" if is_coles else "Woolworths"

    points = [{"x": fmt(dt, "%d-%b"), "y": price} for dt, price in rows]

    if not points:
        return HTMLResponse('<div class="bg-gray-50 px-6 py-3 text-xs text-gray-400">No price history recorded yet.</div>')

    colour = COLES_COLOUR if is_coles else WOOLWORTHS_COLOUR
    html = templates.env.get_template("_chart_single.html").render(
        canvas_id=f"pchart-{product_id}",
        points=points,
        colour=colour,
        label=label,
    )
    return HTMLResponse(html)


@router.get("/history/{match_id}")
async def price_history(match_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Return a chart + table of price history for a matched product pair."""
    match = await session.get(ProductMatch, match_id, options=[selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b)])
    if not match:
        return HTMLResponse("")

    pa, pb = match.product_a, match.product_b
    coles_p = pa if pa.store == Store.COLES else pb
    ww_p = pa if pa.store == Store.WOOLWORTHS else pb

    coles_rows = (await session.execute(
        select(sqlfunc.date(PriceHistory.recorded_at), sqlfunc.avg(PriceHistory.price))
        .where(PriceHistory.product_id == coles_p.id)
        .group_by(sqlfunc.date(PriceHistory.recorded_at))
        .order_by(sqlfunc.date(PriceHistory.recorded_at))
    )).all()

    ww_rows = (await session.execute(
        select(sqlfunc.date(PriceHistory.recorded_at), sqlfunc.avg(PriceHistory.price))
        .where(PriceHistory.product_id == ww_p.id)
        .group_by(sqlfunc.date(PriceHistory.recorded_at))
        .order_by(sqlfunc.date(PriceHistory.recorded_at))
    )).all()

    def fmt(dt_str, f): return (dt_str if isinstance(dt_str, date_type) else date_type.fromisoformat(dt_str)).strftime(f)

    coles_by_date = {dt: price for dt, price in coles_rows}
    ww_by_date = {dt: price for dt, price in ww_rows}

    coles_points = [{"x": fmt(dt, "%d-%b"), "y": price} for dt, price in coles_rows]
    ww_points = [{"x": fmt(dt, "%d-%b"), "y": price} for dt, price in ww_rows]

    # Points where both stores have the same price on the same date
    equal_points = [
        {"x": fmt(dt, "%d-%b"), "y": price}
        for dt, price in coles_by_date.items()
        if dt in ww_by_date and abs(price - ww_by_date[dt]) < 0.001
    ]
    equal_labels = [p["x"] for p in equal_points]

    if not coles_points and not ww_points:
        return HTMLResponse('<div class="bg-gray-50 px-6 py-3 text-xs text-gray-400">No price history recorded yet.</div>')

    # Merge and sort by ISO date string (chronological) before formatting for display
    all_combined = [{"x": fmt(dt, "%d-%b"), "y": price}
                    for dt, price in sorted(list(coles_rows) + list(ww_rows), key=lambda r: r[0])]

    html = templates.env.get_template("_chart_match.html").render(
        canvas_id=f"chart-{match_id}",
        coles_points=coles_points,
        ww_points=ww_points,
        equal_points=equal_points,
        equal_labels=equal_labels,
        all_combined=all_combined,
        coles_colour=COLES_COLOUR,
        ww_colour=WOOLWORTHS_COLOUR,
        price_line_colour=PRICE_LINE_COLOUR,
    )
    return HTMLResponse(html)
