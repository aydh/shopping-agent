"""Product lookup page view."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...templating import templates

router = APIRouter()


@router.get("/product-lookup")
async def product_lookup_page(request: Request) -> HTMLResponse:
    """Render the product lookup page."""
    return templates.TemplateResponse(
        "product_lookup.html",
        {
            "request": request,
            "active_page": "product_lookup",
        },
    )
