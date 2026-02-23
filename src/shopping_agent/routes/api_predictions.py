from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..services.prediction import refresh_predictions

router = APIRouter()


@router.post("/refresh")
async def refresh(session: AsyncSession = Depends(get_session)):
    count = await refresh_predictions(session)
    return HTMLResponse(
        f'<div class="text-green-600 text-sm mb-4">Refreshed {count} predictions. '
        f'<a href="/predictions" class="underline">Reload page</a> to see updates.</div>'
    )
