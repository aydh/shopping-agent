from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import ConsumptionPrediction
from ..services.prediction import refresh_predictions
from ..templating import templates
from ..services.prediction import get_predictions_with_match_info as _predictions_list

router = APIRouter()


@router.post("/refresh")
async def refresh(session: AsyncSession = Depends(get_session)):
    await refresh_predictions(session)
    predictions = await _predictions_list(session)
    html = templates.get_template("_predictions_grid.html").render(predictions=predictions)
    return HTMLResponse(html)


@router.delete("/purge")
async def purge_predictions(session: AsyncSession = Depends(get_session)):
    result = await session.execute(delete(ConsumptionPrediction))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} predictions.</span>'
    )
