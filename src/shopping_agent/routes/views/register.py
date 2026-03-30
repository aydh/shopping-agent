"""Registration page view."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...config import settings
from ...templating import templates

router = APIRouter()


@router.get("/register")
async def register_page(request: Request) -> HTMLResponse:
    """Render the registration page (no auth required)."""
    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "supabase_url": settings.supabase_url or "",
            "supabase_anon_key": settings.supabase_anon_key or "",
        },
    )
