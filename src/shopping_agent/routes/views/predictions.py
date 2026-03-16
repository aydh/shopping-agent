"""Predictions page view."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...services.prediction import get_predictions_with_match_info
from ...templating import templates

router = APIRouter()


@router.get("/predictions")
async def predictions_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Render the predictions page."""
    predictions = await get_predictions_with_match_info(session)
    return templates.TemplateResponse(
        "predictions.html",
        {"request": request, "active_page": "predictions", "predictions": predictions},
    )
