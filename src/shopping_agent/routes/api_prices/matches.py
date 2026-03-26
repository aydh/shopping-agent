"""Match management — confirm, reject, create, purge."""
import logging

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...auth import CurrentUser, get_current_user
from ...database import get_user_session
from ...models import Order, OrderItem, PriceHistory, Product, ProductMatch, Store
from ...services.price_comparison import match_unmatched_products, matches_to_comparisons
from ...templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/match-products")
async def run_match_products(user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_user_session)) -> HTMLResponse:
    """Run auto-matching for all unmatched products across both stores.

    One pass from Coles→Woolworths is sufficient: each match removes both
    products from the unmatched pool, so a second Woolworths→Coles pass
    would find nothing new.
    """
    total = await match_unmatched_products(session, Store.COLES)
    return HTMLResponse(f'<span class="text-blue-600 text-sm">{total} new match{"es" if total != 1 else ""} found</span>')


@router.post("/confirm-match/{match_id}")
async def confirm_match(match_id: int, user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_user_session)) -> HTMLResponse:
    match = await session.get(ProductMatch, match_id, options=[selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b)])
    if not match:
        return HTMLResponse("")

    match.is_confirmed = True

    # Run last_ordered query before commit — it's an independent read and autoflush
    # will send the UPDATE as part of this execute, saving a round-trip.
    product_ids = [match.product_a_id, match.product_b_id]
    lo_rows = await session.execute(
        select(OrderItem.product_id, func.max(Order.order_date))
        .join(Order, OrderItem.order_id == Order.id)
        .where(OrderItem.product_id.in_(product_ids))
        .group_by(OrderItem.product_id)
    )
    last_ordered = dict(lo_rows.all())
    await session.commit()

    comp = matches_to_comparisons([match])[0]
    html = templates.env.get_template("_match_row.html").render(comp=comp, last_ordered=last_ordered)
    return HTMLResponse(html)


@router.post("/manual-match")
async def create_manual_match(
    coles_id: int = Form(...),
    woolworths_id: int = Form(...),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_user_session),
) -> Response:
    """Create a manual match between a Coles and Woolworths product."""
    coles_product = await session.get(Product, coles_id)
    ww_product = await session.get(Product, woolworths_id)

    if not coles_product or not ww_product:
        return HTMLResponse('<p class="text-red-600 text-sm">Product not found.</p>', status_code=400)
    if coles_product.store != Store.COLES or ww_product.store != Store.WOOLWORTHS:
        return HTMLResponse('<p class="text-red-600 text-sm">Select one Coles and one Woolworths product.</p>', status_code=400)

    # Check if a match already exists for either product
    existing = await session.execute(
        select(ProductMatch).where(
            (ProductMatch.product_a_id == coles_id) | (ProductMatch.product_b_id == coles_id) |
            (ProductMatch.product_a_id == woolworths_id) | (ProductMatch.product_b_id == woolworths_id),
            ProductMatch.is_rejected == False,  # noqa: E712
        )
    )
    if existing.scalars().first():
        return HTMLResponse('<p class="text-red-600 text-sm">One of these products is already matched. Remove the existing match first.</p>', status_code=400)

    pm = ProductMatch(
        product_a_id=coles_id,
        product_b_id=woolworths_id,
        confidence=1.0,
        match_method="manual",
        is_confirmed=True,
    )
    session.add(pm)
    await session.commit()

    response = Response(status_code=200)
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/match/{match_id}/undo")
async def undo_rejected_match(match_id: int, user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_user_session)) -> HTMLResponse:
    """Restore a rejected match."""
    match = await session.get(ProductMatch, match_id)
    if not match:
        return HTMLResponse("")
    match.is_rejected = False
    await session.commit()
    return HTMLResponse("")


@router.delete("/matches/purge")
async def purge_all_matches(user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_user_session)) -> HTMLResponse:
    result = await session.execute(delete(ProductMatch))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} product matches.</span>'
    )


@router.delete("/history/purge")
async def purge_price_history(user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_user_session)) -> HTMLResponse:
    result = await session.execute(delete(PriceHistory))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} price history records.</span>'
    )


@router.delete("/match/{match_id}")
async def delete_match(match_id: int, user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_user_session)) -> HTMLResponse:
    """Reject a product match so it is never auto-matched again."""
    result = await session.execute(
        update(ProductMatch)
        .where(ProductMatch.id == match_id)
        .values(is_rejected=True, is_confirmed=False)
    )
    if result.rowcount == 0:
        return HTMLResponse("", status_code=404)
    await session.commit()
    return HTMLResponse("")
