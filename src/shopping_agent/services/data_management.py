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
    return {
        "coles_orders": (await session.execute(select(func.count(Order.id)).where(Order.store == Store.COLES))).scalar() or 0,
        "coles_order_items": (await session.execute(
            select(func.count(OrderItem.id)).join(Order).where(Order.store == Store.COLES)
        )).scalar() or 0,
        "woolworths_orders": (await session.execute(select(func.count(Order.id)).where(Order.store == Store.WOOLWORTHS))).scalar() or 0,
        "woolworths_order_items": (await session.execute(
            select(func.count(OrderItem.id)).join(Order).where(Order.store == Store.WOOLWORTHS)
        )).scalar() or 0,
        "coles_products": (await session.execute(select(func.count(Product.id)).where(Product.store == Store.COLES))).scalar() or 0,
        "woolworths_products": (await session.execute(select(func.count(Product.id)).where(Product.store == Store.WOOLWORTHS))).scalar() or 0,
        "product_matches": (await session.execute(select(func.count(ProductMatch.id)))).scalar() or 0,
        "price_history": (await session.execute(select(func.count(PriceHistory.id)))).scalar() or 0,
        "predictions": (await session.execute(select(func.count(ConsumptionPrediction.id)))).scalar() or 0,
        "shopping_lists": (await session.execute(select(func.count(ShoppingList.id)))).scalar() or 0,
        "shopping_list_items": (await session.execute(select(func.count(ShoppingListItem.id)))).scalar() or 0,
    }
