import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    ConsumptionPrediction,
    ListStatus,
    Product,
    ProductMatch,
    ShoppingList,
    ShoppingListItem,
    Store,
)
from .prediction import generate_candidates
from .price_comparison import build_price_map

logger = logging.getLogger(__name__)


def choose_best_store(
    coles_price: float | None,
    woolworths_price: float | None,
    fallback: Store,
) -> Store:
    """Choose the cheapest available store for an item.

    Args:
        coles_price: Current Coles price, or None if unavailable.
        woolworths_price: Current Woolworths price, or None if unavailable.
        fallback: Store to use when neither or only one price is available.

    Returns:
        The cheaper store, or fallback if prices are equal or unavailable.
    """
    if coles_price is not None and woolworths_price is not None:
        return Store.COLES if coles_price <= woolworths_price else Store.WOOLWORTHS
    if coles_price is not None:
        return Store.COLES
    if woolworths_price is not None:
        return Store.WOOLWORTHS
    return fallback


async def generate_shopping_list(
    session: AsyncSession,
    target_date: date | None = None,
    lookahead_days: int = 7,
) -> ShoppingList:
    """Generate a shopping list based on consumption predictions."""
    target_date = target_date or date.today()
    list_name = f"Week of {target_date.isoformat()}"

    # Get all predictions
    result = await session.execute(
        select(ConsumptionPrediction).options(
            selectinload(ConsumptionPrediction.product)
        )
    )
    predictions = list(result.scalars().all())

    # Generate candidates
    candidates = generate_candidates(
        predictions, target_date=target_date, lookahead_days=lookahead_days
    )

    # Build a price lookup across matched products: product_id -> {coles_price, woolworths_price}
    matches_result = await session.execute(
        select(ProductMatch).options(
            selectinload(ProductMatch.product_a),
            selectinload(ProductMatch.product_b),
        )
    )
    price_map = build_price_map(list(matches_result.scalars().all()))

    # Create or update shopping list
    existing = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status == ListStatus.DRAFT)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = existing.scalars().first()

    if shopping_list:
        # Clear auto-generated items (fix: load items explicitly, don't rely on lazy relationship)
        items_result = await session.execute(
            select(ShoppingListItem).where(ShoppingListItem.shopping_list_id == shopping_list.id)
        )
        for item in items_result.scalars().all():
            if not item.is_user_added:
                await session.delete(item)
        shopping_list.name = list_name
        shopping_list.target_date = target_date
    else:
        shopping_list = ShoppingList(
            name=list_name,
            target_date=target_date,
            status=ListStatus.DRAFT,
        )
        session.add(shopping_list)
        await session.flush()

    # Add candidates
    for candidate in candidates:
        product = await session.get(Product, candidate.product_id)
        if not product:
            continue

        # Use match price map if available, otherwise fall back to the product's own price
        if candidate.product_id in price_map:
            prices = price_map[candidate.product_id]
            coles_price = prices["coles_price"]
            woolworths_price = prices["woolworths_price"]
            # Default to cheapest store, or the store this product is from
            chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
        else:
            coles_price = product.current_price if product.store == Store.COLES else None
            woolworths_price = product.current_price if product.store == Store.WOOLWORTHS else None
            chosen_store = product.store

        item = ShoppingListItem(
            shopping_list_id=shopping_list.id,
            product_id=candidate.product_id,
            quantity=candidate.quantity,
            reason=candidate.reason,
            coles_price=coles_price,
            woolworths_price=woolworths_price,
            chosen_store=chosen_store,
        )
        session.add(item)

    await session.commit()
    logger.info("Generated shopping list with %d items", len(candidates))
    return shopping_list


async def update_item_quantity(
    session: AsyncSession, item_id: int, quantity: int
) -> ShoppingListItem | None:
    item = await session.get(ShoppingListItem, item_id)
    if item:
        if quantity <= 0:
            item.is_removed = True
        else:
            item.quantity = quantity
        await session.commit()
    return item


async def update_item_store(
    session: AsyncSession, item_id: int, store: Store
) -> ShoppingListItem | None:
    item = await session.get(ShoppingListItem, item_id)
    if item:
        item.chosen_store = store
        await session.commit()
    return item


async def remove_item(session: AsyncSession, item_id: int) -> bool:
    item = await session.get(ShoppingListItem, item_id)
    if item:
        item.is_removed = True
        await session.commit()
        return True
    return False


async def get_active_list(session: AsyncSession) -> ShoppingList | None:
    result = await session.execute(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    return result.scalars().first()


async def confirm_list(session: AsyncSession, list_id: int) -> ShoppingList | None:
    shopping_list = await session.get(ShoppingList, list_id)
    if shopping_list:
        shopping_list.status = ListStatus.CONFIRMED
        await session.commit()
    return shopping_list
