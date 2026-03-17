"""Data management service — DB record counts for the settings page."""
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    ConsumptionPrediction,
    Order,
    OrderItem,
    PriceHistory,
    Product,
    ProductMatch,
    ShoppingList,
    ShoppingListItem,
    Store,
)

logger = logging.getLogger(__name__)


async def get_db_counts(session: AsyncSession) -> dict[str, int]:
    """Return record counts for all major tables.

    Args:
        session: Async database session.

    Returns:
        Dict of table-name to record count.
    """
    # Orders per store — single GROUP BY query
    order_rows = {
        row.store: row[1]
        for row in (await session.execute(
            select(Order.store, func.count(Order.id)).group_by(Order.store)
        )).all()
    }

    # Order items per store — single GROUP BY query
    order_item_rows = {
        row.store: row[1]
        for row in (await session.execute(
            select(Order.store, func.count(OrderItem.id))
            .join(OrderItem, OrderItem.order_id == Order.id)
            .group_by(Order.store)
        )).all()
    }

    # Products per store — single GROUP BY query
    product_rows = {
        row.store: row[1]
        for row in (await session.execute(
            select(Product.store, func.count(Product.id)).group_by(Product.store)
        )).all()
    }

    product_matches = (await session.execute(select(func.count(ProductMatch.id)))).scalar() or 0
    price_history = (await session.execute(select(func.count(PriceHistory.id)))).scalar() or 0
    predictions = (await session.execute(select(func.count(ConsumptionPrediction.id)))).scalar() or 0
    shopping_lists = (await session.execute(select(func.count(ShoppingList.id)))).scalar() or 0
    shopping_list_items = (await session.execute(select(func.count(ShoppingListItem.id)))).scalar() or 0

    return {
        "coles_orders": order_rows.get(Store.COLES, 0),
        "woolworths_orders": order_rows.get(Store.WOOLWORTHS, 0),
        "coles_order_items": order_item_rows.get(Store.COLES, 0),
        "woolworths_order_items": order_item_rows.get(Store.WOOLWORTHS, 0),
        "coles_products": product_rows.get(Store.COLES, 0),
        "woolworths_products": product_rows.get(Store.WOOLWORTHS, 0),
        "product_matches": product_matches,
        "price_history": price_history,
        "predictions": predictions,
        "shopping_lists": shopping_lists,
        "shopping_list_items": shopping_list_items,
    }
