import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..auth import (
    CurrentUser,
    _decode_token,
    get_current_user_from_cookie,
)
from ..db_helpers import store_from_string
from ..models import Store
from ..scrapers.registry import get_scraper

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/session")
async def set_session(request: Request) -> JSONResponse:
    """Receive access_token from JS and set an httpOnly cookie."""
    body = await request.json()
    token = body.get("access_token", "")
    if not token:
        return JSONResponse({"error": "access_token required"}, status_code=400)
    try:
        _decode_token(token)
    except HTTPException as exc:
        logger.warning("JWT verification failed: %s", exc.detail)
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    except Exception:
        logger.exception("Unexpected error verifying JWT")
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        key="sb-access-token",
        value=token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=3600,
    )
    return response


@router.post("/logout")
async def logout_session(request: Request) -> Response:
    """Clear the httpOnly session cookie."""
    response = Response(status_code=204)
    response.delete_cookie(
        key="sb-access-token",
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return response


@router.post("/login/{store}")
async def login(store: str) -> HTMLResponse:
    """Login is handled via cookie import for both stores."""
    return HTMLResponse(
        '<span class="text-yellow-600">Use "Import Cookies" below to connect</span>'
    )


_MAX_COOKIE_BODY = 1_000_000  # 1 MB


@router.post("/import-cookies/{store}")
async def import_cookies(
    store: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    """Import cookies from browser DevTools or Cookie-Editor extension."""
    store_enum = store_from_string(store)
    # Read body with a hard cap regardless of Content-Length or chunked encoding
    chunks: list[bytes] = []
    bytes_read = 0
    async for chunk in request.stream():
        bytes_read += len(chunk)
        if bytes_read > _MAX_COOKIE_BODY:
            return HTMLResponse('<span class="text-red-600">Cookie data too large (max 1 MB)</span>')
        chunks.append(chunk)
    body = b"".join(chunks).decode("utf-8")

    scraper = get_scraper(user.user_id, store_enum)

    if store_enum in (Store.WOOLWORTHS, Store.COLES):
        success = await scraper.import_cookies(body)
        if success:
            return HTMLResponse(
                '<span class="text-green-600">Connected - cookies imported</span>'
            )
        return HTMLResponse(
            '<span class="text-red-600">Invalid cookie data - paste the JSON array from Cookie-Editor</span>'
        )

    return HTMLResponse(
        '<span class="text-red-600">Cookie import not supported for this store</span>'
    )


@router.get("/validate/{store}")
async def validate(
    store: str,
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    """Test stored cookies against the live API and report the result."""
    store_enum = store_from_string(store)
    scraper = get_scraper(user.user_id, store_enum)

    if store_enum in (Store.COLES, Store.WOOLWORTHS):
        result = await scraper.validate_cookies()
    else:
        result = {"ok": False, "detail": "Unknown store"}

    if result["ok"]:
        return HTMLResponse(
            f'<span class="text-green-600 text-sm">&#10003; Valid — {result["detail"]}</span>'
        )
    return HTMLResponse(
        f'<span class="text-red-600 text-sm">&#10007; Failed — {result["detail"]}</span>'
    )


@router.post("/logout/{store}")
async def logout_store(
    store: str,
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    store_enum = store_from_string(store)
    scraper = get_scraper(user.user_id, store_enum)
    if store_enum in (Store.WOOLWORTHS, Store.COLES):
        await scraper.logout()
    return HTMLResponse('<span class="text-gray-500">Not connected</span>')
