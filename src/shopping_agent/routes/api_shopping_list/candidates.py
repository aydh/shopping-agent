"""Shopping list candidate generation — generate from predictions, add predictions."""
from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import async_session, get_user_session_from_cookie, set_rls_claims
from ...models import ConsumptionPrediction, Product, ProductMatch, ShoppingList, ShoppingListItem, Store, ListStatus
from ...services.prediction import generate_candidates
from ...services.price_comparison import build_price_map
from ...services.shopping_list import (
    choose_best_store,
    generate_shopping_list,
    get_shopping_list_context as _shopping_list_context,
)
from ...templating import templates

router = APIRouter()


@router.post("/add-predictions")
async def add_predictions(
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Add predicted items to the current active list without replacing existing items."""
    active = (await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )).scalars().first()

    if not active:
        ctx = await _shopping_list_context(session, user.user_id)
        list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
        return HTMLResponse(list_html)

    predictions = list((await session.execute(
        select(ConsumptionPrediction).options(selectinload(ConsumptionPrediction.product))
    )).scalars().all())

    candidates = generate_candidates(predictions, target_date=date.today(), lookahead_days=7)

    # Build price map from matches
    matches = (await session.execute(
        select(ProductMatch)
        .options(selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b))
        .where(ProductMatch.is_rejected == False)  # noqa: E712
    )).scalars().all()
    price_map = build_price_map(list(matches))

    # Existing product ids in the active list
    existing_ids = {r[0] for r in (await session.execute(
        select(ShoppingListItem.product_id).where(
            ShoppingListItem.shopping_list_id == active.id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )).all()}

    for candidate in candidates:
        if candidate.product_id in existing_ids:
            continue
        product = await session.get(Product, candidate.product_id)
        if not product:
            continue
        if candidate.product_id in price_map:
            prices = price_map[candidate.product_id]
            coles_price = prices["coles_price"]
            woolworths_price = prices["woolworths_price"]
            chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
        else:
            coles_price = product.current_price if product.store == Store.COLES else None
            woolworths_price = product.current_price if product.store == Store.WOOLWORTHS else None
            chosen_store = product.store
        session.add(ShoppingListItem(
            shopping_list_id=active.id,
            product_id=candidate.product_id,
            quantity=candidate.quantity,
            reason=candidate.reason,
            coles_price=coles_price,
            woolworths_price=woolworths_price,
            chosen_store=chosen_store,
        ))

    # session.commit() removed: session.begin() context manager commits on exit;
    # autoflush ensures the query below sees pending additions.
    ctx = await _shopping_list_context(session, user.user_id)
    list_html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(list_html)


@router.post("/generate")
async def generate(
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    await generate_shopping_list(session, user.user_id)
    # generate_shopping_list commits internally; read context in a fresh session.
    async with async_session() as fresh:
        async with fresh.begin():
            await set_rls_claims(fresh, user.user_id)
            ctx = await _shopping_list_context(fresh, user.user_id)
    html = templates.get_template("_shopping_list_content.html").render(**ctx)
    return HTMLResponse(html)
