import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

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

# A compact JWT is three base64url segments separated by dots. Anything else is
# rejected before the value reaches a Set-Cookie header, so no control characters
# (CR/LF) can be smuggled into the response header (cookie injection, CWE-20).
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


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
    if not _JWT_RE.match(token):
        return JSONResponse({"error": "Invalid token"}, status_code=400)
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

    # Validate JSON structure before passing to scraper
    try:
        cookie_data = json.loads(body)
        if not isinstance(cookie_data, list):
            return HTMLResponse(
                '<span class="text-red-600">Cookie data must be a JSON array</span>'
            )
        if not cookie_data:
            return HTMLResponse(
                '<span class="text-red-600">Cookie array cannot be empty</span>'
            )
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON in cookie import: %s", e)
        return HTMLResponse(
            '<span class="text-red-600">Invalid JSON format - paste the JSON array from Cookie-Editor</span>'
        )

    scraper = get_scraper(user.user_id, store_enum)

    if store_enum in (Store.WOOLWORTHS, Store.COLES):
        success = await scraper.import_cookies(body)
        if success:
            return HTMLResponse(
                '<span class="text-green-600">Connected - cookies imported</span>'
            )
        return HTMLResponse(
            '<span class="text-red-600">Invalid cookie data - missing required fields (name, value, domain, path)</span>'
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


@router.post("/login-playwright/{store}")
async def login_playwright(
    store: str,
    email: str = Form(...),
    password: str = Form(...),
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> StreamingResponse:
    """Start a Playwright-based login and stream progress as SSE events.

    Events:
      progress  {"message": str}        – status update
      done      {"result": str}         – "ok", "mfa_required", or "failed:…"
    """
    store_enum = store_from_string(store)
    if store_enum not in (Store.COLES, Store.WOOLWORTHS):
        async def _unsupported():
            yield f'event: done\ndata: {json.dumps({"result": "failed:Playwright login is not supported for this store"})}\n\n'
        return StreamingResponse(_unsupported(), media_type="text/event-stream")

    scraper = get_scraper(user.user_id, store_enum)
    queue: asyncio.Queue[str] = asyncio.Queue()

    async def _run() -> None:
        result = await scraper.login_with_credentials(email, password, on_progress=queue.put_nowait)
        await queue.put(f"\x00{result}")  # sentinel prefix distinguishes final result

    asyncio.create_task(_run())

    async def generate():
        try:
            while True:
                msg = await asyncio.wait_for(queue.get(), timeout=120.0)
                if msg.startswith("\x00"):
                    yield f'event: done\ndata: {json.dumps({"result": msg[1:]})}\n\n'
                    break
                yield f'event: progress\ndata: {json.dumps({"message": msg})}\n\n'
        except asyncio.TimeoutError:
            yield f'event: done\ndata: {json.dumps({"result": "failed:Timed out waiting for browser"})}\n\n'

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/login-playwright/{store}/mfa")
async def login_playwright_mfa(
    store: str,
    code: str = Form(...),
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> StreamingResponse:
    """Submit the MFA code and stream the result as a single SSE done event."""
    store_enum = store_from_string(store)
    scraper = get_scraper(user.user_id, store_enum)
    result = await scraper.complete_mfa(code.strip())

    async def generate():
        yield f'event: done\ndata: {json.dumps({"result": result})}\n\n'

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/login-playwright/{store}/cancel")
async def login_playwright_cancel(
    store: str,
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> HTMLResponse:
    """Cancel a pending Playwright login session."""
    store_enum = store_from_string(store)
    scraper = get_scraper(user.user_id, store_enum)
    await scraper.cancel_pending_login()
    return HTMLResponse('<span class="text-gray-500 text-sm">Login cancelled</span>')
