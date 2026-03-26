from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser, get_current_user
from ..database import get_user_session
from ..models import ConsumptionPrediction
from ..services.prediction import refresh_predictions
from ..templating import templates
from ..services.prediction import get_predictions_with_match_info as _predictions_list

router = APIRouter()


@router.post("/refresh")
async def refresh(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_user_session),
) -> HTMLResponse:
    await refresh_predictions(session, user.user_id)
    predictions = await _predictions_list(session, user.user_id)
    html = templates.get_template("_predictions_grid.html").render(predictions=predictions)
    return HTMLResponse(html)


@router.delete("/purge")
async def purge_predictions(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_user_session),
) -> HTMLResponse:
    result = await session.execute(delete(ConsumptionPrediction))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} predictions.</span>'
    )
