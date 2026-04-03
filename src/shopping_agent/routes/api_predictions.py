from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser, get_current_user_from_cookie
from ..database import async_session, get_user_session_from_cookie, set_rls_claims
from ..models import ConsumptionPrediction
from ..services.prediction import refresh_predictions
from ..templating import templates
from ..services.prediction import get_predictions_with_match_info as _predictions_list

router = APIRouter()


@router.post("/refresh")
async def refresh(
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    await refresh_predictions(session, user.user_id)
    # refresh_predictions commits its own transaction; read in a fresh session
    async with async_session() as fresh_session:
        async with fresh_session.begin():
            await set_rls_claims(fresh_session, user.user_id)
            predictions = await _predictions_list(fresh_session, user.user_id)
    html = templates.get_template("_predictions_grid.html").render(predictions=predictions)
    return HTMLResponse(html)


@router.delete("/purge")
async def purge_predictions(
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    result = await session.execute(delete(ConsumptionPrediction))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} predictions.</span>'  # type: ignore[attr-defined]
    )
