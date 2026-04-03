"""Prices page view."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import get_user_session_from_cookie
from ...db_helpers import visible_products_query
from ...models import Order, OrderItem, Product, ProductMatch, Store, UserProductPreferences
from ...services.price_comparison import matches_to_comparisons
from ...templating import templates

router = APIRouter()


@router.get("/prices")
async def prices_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Render the price comparison page."""
    # Fetch all visible products
    result = await session.execute(
        visible_products_query(user.user_id)
        .order_by(Product.store, Product.name)
    )
    all_products = list(result.scalars().all())
    visible_ids = {p.id for p in all_products}

    # Fetch active matches (exclude rejected, exclude matches where either product is hidden)
    match_result = await session.execute(
        select(ProductMatch)
        .options(selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b))
        .where(ProductMatch.is_rejected == False)  # noqa: E712
        .order_by(ProductMatch.confidence.desc())
    )
    matches = [
        m for m in match_result.scalars().all()
        if m.product_a_id in visible_ids and m.product_b_id in visible_ids
    ]
    comparisons = matches_to_comparisons(matches)

    matched_ids: set[int] = set()
    for m in matches:
        matched_ids.add(m.product_a_id)
        matched_ids.add(m.product_b_id)

    unmatched_coles = [p for p in all_products if p.store == Store.COLES and p.id not in matched_ids]
    unmatched_woolworths = [p for p in all_products if p.store == Store.WOOLWORTHS and p.id not in matched_ids]

    # Last ordered date for all visible products (single query)
    lo_rows = await session.execute(
        select(OrderItem.product_id, func.max(Order.order_date))
        .join(Order, OrderItem.order_id == Order.id)
        .where(OrderItem.product_id.in_(visible_ids))
        .group_by(OrderItem.product_id)
    )
    last_ordered: dict[int, date] = dict(lo_rows.all())  # type: ignore[arg-type]

    # Fetch rejected matches
    rejected_result = await session.execute(
        select(ProductMatch)
        .options(selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b))
        .where(ProductMatch.is_rejected == True)  # noqa: E712
        .order_by(ProductMatch.updated_at.desc())
    )
    rejected_matches = rejected_result.scalars().all()

    # Fetch hidden products (per this user)
    hidden_result = await session.execute(
        select(Product)
        .join(
            UserProductPreferences,
            (UserProductPreferences.product_id == Product.id)
            & (UserProductPreferences.user_id == user.user_id)
            & UserProductPreferences.is_hidden.is_(True),
        )
        .order_by(Product.store, Product.name)
    )
    hidden_products = list(hidden_result.scalars().all())

    # Last ordered date for hidden products (single aggregation query)
    hidden_ids = {p.id for p in hidden_products}
    if hidden_ids:
        hidden_lo_rows = await session.execute(
            select(OrderItem.product_id, func.max(Order.order_date))
            .join(Order, OrderItem.order_id == Order.id)
            .where(OrderItem.product_id.in_(hidden_ids))
            .group_by(OrderItem.product_id)
        )
        hidden_last_ordered: dict[int, date] = dict(hidden_lo_rows.all())  # type: ignore[arg-type]
    else:
        hidden_last_ordered = {}
    for p in hidden_products:
        setattr(p, "last_ordered_date", hidden_last_ordered.get(p.id))

    # Fetch unavailable products (is_available=False, not hidden by this user)
    unavailable_result = await session.execute(
        visible_products_query(user.user_id)
        .where(Product.is_available == False)  # noqa: E712
        .order_by(Product.store, Product.name)
    )
    unavailable_products = list(unavailable_result.scalars().all())

    return templates.TemplateResponse(
        request,
        "prices.html",
        {
            "active_page": "prices",
            "comparisons": comparisons,
            "unmatched_coles": unmatched_coles,
            "unmatched_woolworths": unmatched_woolworths,
            "rejected_matches": rejected_matches,
            "hidden_products": hidden_products,
            "unavailable_products": unavailable_products,
            "last_ordered": last_ordered,
        },
    )


@router.get("/prices/search-match/{product_id}")
async def search_match_page(
    product_id: int,
    request: Request,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Render the manual search-match page for a given product."""
    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse("Product not found", status_code=404)
    target_store = Store.WOOLWORTHS if product.store == Store.COLES else Store.COLES
    return templates.TemplateResponse(
        request,
        "search_match.html",
        {
            "active_page": "prices",
            "product": product,
            "target_store": target_store.value,
        },
    )
