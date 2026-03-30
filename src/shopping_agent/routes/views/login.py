"""Login page view."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...config import settings
from ...templating import templates

router = APIRouter()


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    """Render the login page (no auth required)."""
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "supabase_url": settings.supabase_url or "",
            "supabase_anon_key": settings.supabase_anon_key or "",
        },
    )
