from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from ..models.product import Store
from ..scrapers.browser_manager import browser_manager

router = APIRouter()


@router.post("/login/{store}")
async def login(store: str):
    store_enum = Store(store)
    success = await browser_manager.login_interactive(store_enum)
    if success:
        return HTMLResponse(
            f'<span class="text-green-600">Connected - session active</span>'
        )
    return HTMLResponse(
        f'<span class="text-red-600">Login failed or was cancelled</span>'
    )


@router.post("/logout/{store}")
async def logout(store: str):
    store_enum = Store(store)
    await browser_manager.logout(store_enum)
    return HTMLResponse('<span class="text-gray-500">Not connected</span>')
