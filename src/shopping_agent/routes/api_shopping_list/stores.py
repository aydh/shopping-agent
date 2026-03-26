"""Shopping list store selection — set store for all items, submit by store or split."""
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import get_user_session_from_cookie
from ...db_helpers import store_from_string
from ...models import ListStatus, ShoppingList, ShoppingListItem, Store
from ...services.shopping_list import (
    assign_cheapest_stores,
    get_shopping_list_context as _shopping_list_context,
)
from ...templating import templates

router = APIRouter()


@router.post("/set-store/{store}")
async def set_all_store(
    store: str,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Set all items in the active list to the given store."""
    store_enum = store_from_string(store)
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if shopping_list:
        items_result = await session.execute(
            select(ShoppingListItem).where(
                ShoppingListItem.shopping_list_id == shopping_list.id,
                ShoppingListItem.is_removed == False,  # noqa: E712
            )
        )
        for item in items_result.scalars().all():
            item.chosen_store = store_enum
        await session.commit()

    ctx = await _shopping_list_context(session, user.user_id)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)


@router.post("/submit-store/{store}")
async def submit_store(
    store: str,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> RedirectResponse:
    """Set all items to a single store, confirm, and redirect to review."""
    store_enum = store_from_string(store)
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if not shopping_list:
        return RedirectResponse("/shopping-list", status_code=303)
    items_result = await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == shopping_list.id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )
    for item in items_result.scalars().all():
        item.chosen_store = store_enum
    shopping_list.status = ListStatus.CONFIRMED
    await session.commit()
    return RedirectResponse("/confirm", status_code=303)


@router.post("/submit-split")
async def submit_split(
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> RedirectResponse:
    """Set each item to its cheapest available store, confirm, and redirect to review."""
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if not shopping_list:
        return RedirectResponse("/shopping-list", status_code=303)
    await assign_cheapest_stores(session, user.user_id)
    shopping_list.status = ListStatus.CONFIRMED
    await session.commit()
    return RedirectResponse("/confirm", status_code=303)
