"""OAuth consent screen — rendered when Supabase redirects here during authorization."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...config import settings
from ...templating import templates

router = APIRouter()


@router.get("/oauth/consent", response_model=None)
async def oauth_consent_page(request: Request) -> HTMLResponse:
    """Render the OAuth consent screen.

    Supabase redirects here after authenticating the user, passing an
    authorization_id that the Supabase JS SDK uses to fetch client details
    and complete the approval or denial.

    Authentication is checked client-side: the JS checks for an active
    Supabase session in localStorage (established during Supabase's hosted
    login) and redirects to /login only if none exists. This preserves the
    original session so approveAuthorization() can succeed.
    """
    authorization_id = request.query_params.get("authorization_id", "")
    return templates.TemplateResponse(
        request,
        "oauth_consent.html",
        {
            "authorization_id": authorization_id,
            "supabase_url": settings.supabase_url or "",
            "supabase_anon_key": settings.supabase_anon_key or "",
        },
    )
