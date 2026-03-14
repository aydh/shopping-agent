from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import ConsumptionPrediction
from ..services.prediction import refresh_predictions

router = APIRouter()


@router.post("/refresh")
async def refresh(session: AsyncSession = Depends(get_session)):
    count = await refresh_predictions(session)
    return HTMLResponse(
        f'<div class="text-green-600 text-sm mb-4">Refreshed {count} predictions. '
        f'<a href="/predictions" class="underline">Reload page</a> to see updates.</div>'
    )


@router.delete("/purge")
async def purge_predictions(session: AsyncSession = Depends(get_session)):
    result = await session.execute(delete(ConsumptionPrediction))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} predictions.</span>'
    )
