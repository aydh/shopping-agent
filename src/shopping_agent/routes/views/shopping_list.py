"""Shopping list and confirm page views."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...database import get_session
from ...models import ListStatus, Product, ShoppingList, ShoppingListItem, Store
from ...services.shopping_list import (
    get_list_history,
    get_shopping_list_context,
    resolve_display_names,
)
from ...templating import templates

router = APIRouter()


@router.get("/shopping-list")
async def shopping_list_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Render the shopping list page."""
    ctx = await get_shopping_list_context(session)
    past_lists = await get_list_history(session)
    return templates.TemplateResponse(
        request,
        "shopping_list.html",
        {"active_page": "shopping_list", "past_lists": past_lists, **ctx},
    )


@router.get("/shopping-list/find-match/{product_id}")
async def find_match_page(
    product_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the find-match page for a shopping list product."""
    product = await session.get(Product, product_id)
    if not product:
        return RedirectResponse("/shopping-list", status_code=303)
    return templates.TemplateResponse(
        request,
        "find_match.html",
        {"active_page": "shopping_list", "product": product},
    )


@router.get("/confirm")
async def confirm_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Render the order confirmation page."""
    query = (
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status == ListStatus.CONFIRMED)
        .order_by(ShoppingList.created_at.desc())
    )
    result = await session.execute(query)
    shopping_list = result.scalars().first()

    coles_items: list[ShoppingListItem] = []
    woolworths_items: list[ShoppingListItem] = []
    coles_total = 0.0
    woolworths_total = 0.0
    display_names: dict[int, str] = {}

    if shopping_list:
        all_items = [i for i in shopping_list.items if not i.is_removed]
        display_names, _, _sp = await resolve_display_names(session, all_items)
        for item in all_items:
            if item.chosen_store == Store.COLES:
                coles_items.append(item)
                coles_total += (item.coles_price or 0) * item.quantity
            else:
                woolworths_items.append(item)
                woolworths_total += (item.woolworths_price or 0) * item.quantity

    return templates.TemplateResponse(
        request,
        "confirm.html",
        {
            "active_page": "confirm",
            "shopping_list": shopping_list,
            "display_names": display_names,
            "coles_items": coles_items,
            "woolworths_items": woolworths_items,
            "coles_total": coles_total,
            "woolworths_total": woolworths_total,
        },
    )
