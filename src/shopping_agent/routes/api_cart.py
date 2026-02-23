from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import Store
from ..services.cart import add_to_cart

router = APIRouter()


@router.post("/add/{store}")
async def add_items_to_cart(store: str, session: AsyncSession = Depends(get_session)):
    store_enum = Store(store)
    result = await add_to_cart(session, store_enum)

    if result["success"]:
        cart_url = result.get("cart_url", "#")
        return HTMLResponse(
            f'<div class="p-3 bg-green-50 border border-green-200 text-green-800 text-sm rounded mt-2">'
            f'{result["message"]}. '
            f'<a href="{cart_url}" target="_blank" class="underline font-medium">'
            f"Open {store} cart to review & submit</a></div>"
        )
    return HTMLResponse(
        f'<div class="p-3 bg-red-50 border border-red-200 text-red-800 text-sm rounded mt-2">'
        f'{result.get("error") or result["message"]}</div>'
    )
