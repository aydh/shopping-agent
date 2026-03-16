"""Orders page view."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...database import get_session
from ...db_helpers import store_from_string
from ...models import Order, Store
from ...templating import templates

router = APIRouter()


@router.get("/orders")
async def orders_page(
    request: Request,
    store: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the orders page, optionally filtered by store."""
    query = select(Order).options(selectinload(Order.items)).order_by(Order.order_date.desc())
    if store in ("coles", "woolworths"):
        query = query.where(Order.store == store_from_string(store))
    result = await session.execute(query)
    orders = result.scalars().all()

    return templates.TemplateResponse(
        "orders.html",
        {
            "request": request,
            "active_page": "orders",
            "orders": orders,
            "store_filter": store or "all",
        },
    )
