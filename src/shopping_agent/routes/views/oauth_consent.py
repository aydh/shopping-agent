"""OAuth consent screen — rendered when Supabase redirects here during authorization."""
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...config import settings
from ...templating import templates

router = APIRouter()


@router.get("/oauth/consent")
async def oauth_consent_page(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the OAuth consent screen.

    Supabase redirects here after authenticating the user, passing an
    authorization_id that the Supabase JS SDK uses to fetch client details
    and complete the approval or denial.

    Requires the user to be logged into our app so that the Supabase JS client
    on the page has an active session when calling approveAuthorization().
    If not authenticated, redirects to /login with a next= redirect back here.
    """
    authorization_id = request.query_params.get("authorization_id", "")

    if not request.cookies.get("sb-access-token"):
        consent_path = f"/oauth/consent?authorization_id={quote(authorization_id, safe='')}"
        return RedirectResponse(
            url=f"/login?next={quote(consent_path, safe='')}",
            status_code=302,
        )

    return templates.TemplateResponse(
        request,
        "oauth_consent.html",
        {
            "authorization_id": authorization_id,
            "supabase_url": settings.supabase_url or "",
            "supabase_anon_key": settings.supabase_anon_key or "",
        },
    )
