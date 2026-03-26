"""Product lookup page view."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...auth import CurrentUser, get_current_user_from_cookie
from ...templating import templates

router = APIRouter()


@router.get("/product-lookup")
async def product_lookup_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    """Render the product lookup page."""
    return templates.TemplateResponse(
        request,
        "product_lookup.html",
        {"active_page": "product_lookup"},
    )
