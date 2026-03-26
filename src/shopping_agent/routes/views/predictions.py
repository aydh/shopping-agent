"""Predictions page view."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import get_user_session_from_cookie
from ...services.prediction import get_predictions_with_match_info
from ...templating import templates

router = APIRouter()


@router.get("/predictions")
async def predictions_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Render the predictions page."""
    predictions = await get_predictions_with_match_info(session, user.user_id)
    return templates.TemplateResponse(
        request,
        "predictions.html",
        {"active_page": "predictions", "predictions": predictions},
    )
