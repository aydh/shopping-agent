"""Auth callback view — handles OAuth and email confirmation redirects."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...config import settings
from ...templating import templates

router = APIRouter()


@router.get("/auth/callback")
async def auth_callback_page(request: Request) -> HTMLResponse:
    """Render the OAuth/email-confirmation callback page."""
    return templates.TemplateResponse(
        request,
        "auth_callback.html",
        {
            "supabase_url": settings.supabase_url or "",
            "supabase_anon_key": settings.supabase_anon_key or "",
        },
    )
