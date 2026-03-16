"""Match management — confirm, reject, create, purge."""
import logging

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...database import get_session
from ...models import PriceHistory, Product, ProductMatch, Store
from ...services.price_comparison import matches_to_comparisons
from ...templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/confirm-match/{match_id}")
async def confirm_match(match_id: int, session: AsyncSession = Depends(get_session)):
    match = await session.get(ProductMatch, match_id, options=[selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b)])
    if not match:
        return HTMLResponse("")

    match.is_confirmed = True
    await session.commit()

    comp = matches_to_comparisons([match])[0]
    html = templates.env.get_template("_match_row.html").render(comp=comp)
    return HTMLResponse(html)


@router.post("/manual-match")
async def create_manual_match(
    coles_id: int = Form(...),
    woolworths_id: int = Form(...),
    session: AsyncSession = Depends(get_session),
):
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
async def undo_rejected_match(match_id: int, session: AsyncSession = Depends(get_session)):
    """Restore a rejected match."""
    match = await session.get(ProductMatch, match_id)
    if not match:
        return HTMLResponse("")
    match.is_rejected = False
    await session.commit()
    return HTMLResponse("")


@router.delete("/matches/purge")
async def purge_all_matches(session: AsyncSession = Depends(get_session)):
    result = await session.execute(delete(ProductMatch))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} product matches.</span>'
    )


@router.delete("/history/purge")
async def purge_price_history(session: AsyncSession = Depends(get_session)):
    result = await session.execute(delete(PriceHistory))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} price history records.</span>'
    )


@router.delete("/match/{match_id}")
async def delete_match(match_id: int, session: AsyncSession = Depends(get_session)):
    """Reject a product match so it is never auto-matched again."""
    match = await session.get(ProductMatch, match_id)
    if not match:
        return HTMLResponse("", status_code=404)
    match.is_rejected = True
    match.is_confirmed = False
    await session.commit()
    return HTMLResponse("")
