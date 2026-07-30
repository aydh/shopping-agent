"""Price history chart endpoints."""
import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import date as date_type

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...config import COLES_COLOUR, WOOLWORTHS_COLOUR
from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import get_user_session_from_cookie
from ...models import PriceHistory, Product, ProductMatch, Store
from ...templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


def _fmt(dt, f: str) -> str:
    if not isinstance(dt, date_type):
        dt = date_type.fromisoformat(dt)
    return dt.strftime(f)


def _expand_rows(rows: Sequence) -> list[dict]:
    """Expand (recorded_at, last_seen_at, price) interval rows into chart points.

    Each price regime contributes its start point and, when the price was last
    confirmed on a later day, an end point — so the chart shows the price
    holding steady between changes rather than a gap.
    """
    points: list[dict] = []
    for recorded_at, last_seen_at, price in rows:
        start = _fmt(recorded_at, "%Y-%m-%d")
        points.append({"x": start, "y": price})
        if last_seen_at is not None:
            end = _fmt(last_seen_at, "%Y-%m-%d")
            if end != start:
                points.append({"x": end, "y": price})
    return points


def _render_single(product_id: int, store: Store, rows: Sequence) -> str:
    points = _expand_rows(rows)
    if not points:
        return '<div class="bg-gray-50 px-6 py-3 text-xs text-gray-400">No price history recorded yet.</div>'
    label = "Coles" if store == Store.COLES else "Woolworths"
    colour = COLES_COLOUR if store == Store.COLES else WOOLWORTHS_COLOUR
    return templates.env.get_template("_chart_single.html").render(
        canvas_id=f"pchart-{product_id}", points=points, colour=colour, label=label
    )


def _render_match(canvas_id: str, coles_rows: list, ww_rows: list) -> str:
    coles_points = _expand_rows(coles_rows)
    ww_points = _expand_rows(ww_rows)
    if not coles_points and not ww_points:
        return '<div class="bg-gray-50 px-6 py-3 text-xs text-gray-400">No price history recorded yet.</div>'
    return templates.env.get_template("_chart_match.html").render(
        canvas_id=canvas_id,
        coles_points=coles_points,
        ww_points=ww_points,
        coles_colour=COLES_COLOUR,
        ww_colour=WOOLWORTHS_COLOUR,
    )


@router.get("/product-history/batch")
async def product_price_history_batch(
    ids: str = Query(..., description="Comma-separated product IDs"),
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> JSONResponse:
    """Return charts for multiple products in one request."""
    product_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    if not product_ids:
        return JSONResponse({})

    # Single query for all price history across all requested products
    ph_rows = (await session.execute(
        select(PriceHistory.product_id, PriceHistory.recorded_at, PriceHistory.last_seen_at, PriceHistory.price)
        .where(PriceHistory.product_id.in_(product_ids))
        .order_by(PriceHistory.product_id, PriceHistory.recorded_at)
    )).all()

    # Single query for all products
    products = {
        p.id: p for p in (await session.execute(
            select(Product).where(Product.id.in_(product_ids))
        )).scalars().all()
    }

    # Group rows by product_id
    rows_by_product: dict[int, list] = defaultdict(list)
    for pid, recorded_at, last_seen_at, price in ph_rows:
        rows_by_product[pid].append((recorded_at, last_seen_at, price))

    result = {}
    for pid in product_ids:
        product = products.get(pid)
        if not product:
            continue
        result[str(pid)] = _render_single(pid, product.store, rows_by_product[pid])

    return JSONResponse(result)


@router.get("/product-history/{product_id}")
async def product_price_history(product_id: int, user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie)) -> HTMLResponse:
    """Return a chart for a single product."""
    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse("")
    rows = (await session.execute(
        select(PriceHistory.recorded_at, PriceHistory.last_seen_at, PriceHistory.price)
        .where(PriceHistory.product_id == product_id)
        .order_by(PriceHistory.recorded_at)
    )).all()
    return HTMLResponse(_render_single(product_id, product.store, rows))


@router.get("/history/batch")
async def price_history_batch(
    ids: str = Query(..., description="Comma-separated match IDs"),
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> JSONResponse:
    """Return charts for multiple matched pairs in one request."""
    match_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    if not match_ids:
        return JSONResponse({})

    matches = (await session.execute(
        select(ProductMatch)
        .options(selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b))
        .where(ProductMatch.id.in_(match_ids))
    )).scalars().all()

    # Collect all product IDs needed
    product_id_pairs: dict[int, tuple[int, int]] = {}  # match_id -> (coles_id, ww_id)
    all_product_ids: list[int] = []
    for m in matches:
        pa, pb = m.product_a, m.product_b
        coles_id = pa.id if pa.store == Store.COLES else pb.id
        ww_id = pa.id if pa.store == Store.WOOLWORTHS else pb.id
        product_id_pairs[m.id] = (coles_id, ww_id)
        all_product_ids.extend([coles_id, ww_id])

    # Single query for all price history
    ph_rows = (await session.execute(
        select(PriceHistory.product_id, PriceHistory.recorded_at, PriceHistory.last_seen_at, PriceHistory.price)
        .where(PriceHistory.product_id.in_(all_product_ids))
        .order_by(PriceHistory.product_id, PriceHistory.recorded_at)
    )).all()

    rows_by_product: dict[int, list] = defaultdict(list)
    for pid, recorded_at, last_seen_at, price in ph_rows:
        rows_by_product[pid].append((recorded_at, last_seen_at, price))

    result = {}
    for m in matches:
        coles_id, ww_id = product_id_pairs[m.id]
        result[str(m.id)] = _render_match(f"chart-{m.id}", rows_by_product[coles_id], rows_by_product[ww_id])

    return JSONResponse(result)


@router.get("/history/{match_id}")
async def price_history(match_id: int, user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie)) -> HTMLResponse:
    """Return a chart for a matched product pair."""
    match = await session.get(ProductMatch, match_id, options=[
        selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b)
    ])
    if not match:
        return HTMLResponse("")
    pa, pb = match.product_a, match.product_b
    coles_p = pa if pa.store == Store.COLES else pb
    ww_p = pa if pa.store == Store.WOOLWORTHS else pb

    coles_rows, ww_rows = await _fetch_match_rows(session, coles_p.id, ww_p.id)
    return HTMLResponse(_render_match(f"chart-{match_id}", coles_rows, ww_rows))


@router.get("/product-pair")
async def product_pair_chart(
    ids: str = Query(..., description="Comma-separated product IDs (exactly 2: coles_id,ww_id)"),
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Return a combined Coles+Woolworths price chart for two product IDs."""
    parts = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    if len(parts) != 2:
        return HTMLResponse("")
    coles_id, ww_id = parts
    coles_rows, ww_rows = await _fetch_match_rows(session, coles_id, ww_id)
    return HTMLResponse(_render_match(f"pair-{coles_id}-{ww_id}", coles_rows, ww_rows))


async def _fetch_match_rows(session: AsyncSession, coles_id: int, ww_id: int):
    coles_rows = (await session.execute(
        select(PriceHistory.recorded_at, PriceHistory.last_seen_at, PriceHistory.price)
        .where(PriceHistory.product_id == coles_id)
        .order_by(PriceHistory.recorded_at)
    )).all()
    ww_rows = (await session.execute(
        select(PriceHistory.recorded_at, PriceHistory.last_seen_at, PriceHistory.price)
        .where(PriceHistory.product_id == ww_id)
        .order_by(PriceHistory.recorded_at)
    )).all()
    return coles_rows, ww_rows
