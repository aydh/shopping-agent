"""Shopping list CRUD — list-level create, read, delete, and details."""
from datetime import date

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import get_user_session_from_cookie
from ...models import ListStatus, Product, ShoppingList, ShoppingListItem
from ...services.shopping_list import (
    confirm_list,
    get_list_history,
    get_shopping_list_context as _shopping_list_context,
)
from ...templating import templates

router = APIRouter()


def _list_header_oob(shopping_list: ShoppingList | None) -> str:
    """Render the OOB list-header fragment."""
    has_list = shopping_list is not None
    return templates.get_template("_list_header.html").render(
        has_list=has_list,
        title=(shopping_list.name if shopping_list is not None else None) or "Shopping List",
        new_cls="bg-gray-200 text-gray-400 cursor-not-allowed" if has_list else "bg-blue-600 text-white hover:bg-blue-700",
        pred_cls="bg-gray-200 text-gray-400 cursor-not-allowed" if not has_list else "bg-green-600 text-white hover:bg-green-700",
        new_disabled="disabled" if has_list else "",
        pred_disabled="disabled" if not has_list else "",
    )


async def _past_lists_section_html(session: AsyncSession, user_id) -> str:
    """Render the Past Shopping Lists section fragment."""
    past_lists = await get_list_history(session, user_id)
    return templates.get_template("_past_lists_section.html").render(past_lists=past_lists)


@router.delete("/current")
async def delete_current_list(
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Delete the current active (non-ordered) shopping list."""
    shopping_list = (await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )).scalars().first()

    if shopping_list:
        await session.execute(
            delete(ShoppingListItem).where(ShoppingListItem.shopping_list_id == shopping_list.id)
        )
        await session.delete(shopping_list)
        # session.begin() context manager commits on exit; autoflush covers pending deletes.

    ctx = await _shopping_list_context(session, user.user_id)
    list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(list_html + _list_header_oob(None))


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _list_name(d: date) -> str:
    return f"Shopping - {d.strftime('%A')} {_ordinal(d.day)} {d.strftime('%B %Y')}"


@router.post("/new")
async def new_list(
    target_date: date | None = Form(default=None),
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Create a new empty shopping list (disabled if one already exists)."""
    existing = (await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )).scalars().first()

    if existing:
        return HTMLResponse("")

    chosen_date = target_date or date.today()
    name = _list_name(chosen_date)
    shopping_list = ShoppingList(name=name, target_date=chosen_date, status=ListStatus.DRAFT, user_id=user.user_id)
    session.add(shopping_list)
    await session.flush()  # assigns the id; context manager commits on exit

    ctx = await _shopping_list_context(session, user.user_id)
    list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(list_html + _list_header_oob(ctx["shopping_list"]))


@router.post("/confirm/{list_id}")
async def confirm(
    list_id: int,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> RedirectResponse:
    await confirm_list(session, list_id)
    return RedirectResponse("/confirm", status_code=303)


@router.post("/close/{list_id}")
async def close_list(
    list_id: int,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> RedirectResponse:
    """Mark the shopping list as ordered (closed)."""
    shopping_list = await session.get(ShoppingList, list_id)
    if shopping_list:
        shopping_list.status = ListStatus.ORDERED
        await session.commit()
    return RedirectResponse("/shopping-list", status_code=303)


@router.get("/details/{list_id}")
async def list_details(
    list_id: int,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Return an HTML fragment listing all items in a past shopping list."""
    items_result = await session.execute(
        select(ShoppingListItem)
        .where(
            ShoppingListItem.shopping_list_id == list_id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )
    items = items_result.scalars().all()
    product_ids = [i.product_id for i in items]

    products_result = await session.execute(
        select(Product).where(Product.id.in_(product_ids))
    )
    products_by_id = {p.id: p for p in products_result.scalars().all()}

    items_data = [
        {
            "name": products_by_id[i.product_id].name,
            "quantity": i.quantity,
            "coles_price": i.coles_price,
            "woolworths_price": i.woolworths_price,
            "product_id": i.product_id,
        }
        for i in items
        if i.product_id in products_by_id
    ]
    html = templates.get_template("_past_list_details.html").render(items=items_data)
    return HTMLResponse(html)


@router.delete("/history/{list_id}")
async def delete_past_list(
    list_id: int,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Delete a past (ordered) shopping list and refresh the history section."""
    shopping_list = await session.get(ShoppingList, list_id)
    if shopping_list and shopping_list.status == ListStatus.ORDERED:
        await session.execute(
            delete(ShoppingListItem).where(ShoppingListItem.shopping_list_id == shopping_list.id)
        )
        await session.delete(shopping_list)
        # session.begin() context manager commits on exit; autoflush covers pending deletes.

    return HTMLResponse(await _past_lists_section_html(session, user.user_id))


@router.delete("/purge")
async def purge_shopping_lists(
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    items = await session.execute(delete(ShoppingListItem))
    lists = await session.execute(delete(ShoppingList))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {lists.rowcount} lists and {items.rowcount} items.</span>'  # type: ignore[attr-defined]
    )
