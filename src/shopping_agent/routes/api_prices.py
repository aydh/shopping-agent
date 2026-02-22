from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import Product, ProductMatch

router = APIRouter()


@router.post("/refresh")
async def refresh_prices(session: AsyncSession = Depends(get_session)):
    # TODO: Implement price refresh via scrapers
    return HTMLResponse(
        '<tr><td colspan="6" class="px-6 py-4 text-center text-green-600 text-sm">'
        'Prices refreshed. <a href="/prices" class="underline">Reload</a> to see updates.</td></tr>'
    )


@router.post("/confirm-match/{match_id}")
async def confirm_match(match_id: int, session: AsyncSession = Depends(get_session)):
    match = await session.get(ProductMatch, match_id)
    if match:
        match.is_confirmed = True
        await session.commit()
        return HTMLResponse(
            '<td colspan="6" class="px-6 py-4 text-center text-green-600 text-sm">'
            'Match confirmed. <a href="/prices" class="underline">Reload</a> to see updates.</td>'
        )
    return HTMLResponse("")
