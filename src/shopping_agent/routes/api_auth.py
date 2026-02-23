from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..models.product import Store
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper

router = APIRouter()


@router.post("/login/{store}")
async def login(store: str):
    """Login is handled via cookie import for both stores."""
    return HTMLResponse(
        '<span class="text-yellow-600">Use "Import Cookies" below to connect</span>'
    )


@router.post("/import-cookies/{store}")
async def import_cookies(store: str, request: Request):
    """Import cookies from browser DevTools or Cookie-Editor extension."""
    store_enum = Store(store)
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


@router.post("/logout/{store}")
async def logout(store: str):
    store_enum = Store(store)
    if store_enum == Store.WOOLWORTHS:
        await woolworths_scraper.logout()
    elif store_enum == Store.COLES:
        await coles_scraper.logout()
    return HTMLResponse('<span class="text-gray-500">Not connected</span>')
