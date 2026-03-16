from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..db_helpers import store_from_string
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper

router = APIRouter()


@router.post("/login/{store}")
async def login(store: str) -> HTMLResponse:
    """Login is handled via cookie import for both stores."""
    return HTMLResponse(
        '<span class="text-yellow-600">Use "Import Cookies" below to connect</span>'
    )


@router.post("/import-cookies/{store}")
async def import_cookies(store: str, request: Request) -> HTMLResponse:
    """Import cookies from browser DevTools or Cookie-Editor extension."""
    store_enum = store_from_string(store)
    body = (await request.body()).decode("utf-8")

    if store_enum == Store.WOOLWORTHS:
        success = await woolworths_scraper.import_cookies(body)
        if success:
            return HTMLResponse(
                '<span class="text-green-600">Connected - cookies imported</span>'
            )
        return HTMLResponse(
            '<span class="text-red-600">Invalid cookie data - paste the JSON array from Cookie-Editor</span>'
        )

    if store_enum == Store.COLES:
        success = await coles_scraper.import_cookies(body)
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
async def validate(store: str) -> HTMLResponse:
    """Test stored cookies against the live API and report the result."""
    store_enum = store_from_string(store)
    if store_enum == Store.COLES:
        result = await coles_scraper.validate_cookies()
    elif store_enum == Store.WOOLWORTHS:
        result = await woolworths_scraper.validate_cookies()
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
async def logout(store: str) -> HTMLResponse:
    store_enum = store_from_string(store)
    if store_enum == Store.WOOLWORTHS:
        await woolworths_scraper.logout()
    elif store_enum == Store.COLES:
        await coles_scraper.logout()
    return HTMLResponse('<span class="text-gray-500">Not connected</span>')
