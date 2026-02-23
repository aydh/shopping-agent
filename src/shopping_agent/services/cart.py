import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import ListStatus, ShoppingList, ShoppingListItem, Store
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper

logger = logging.getLogger(__name__)


async def add_to_cart(session: AsyncSession, store: Store) -> dict:
    """Add confirmed shopping list items to the specified store's cart."""
    result = await session.execute(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status == ListStatus.CONFIRMED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()

    if not shopping_list:
        return {"success": False, "error": "No confirmed shopping list found"}

    # Collect items for this store
    items_to_add = []
    for item in shopping_list.items:
        if item.is_removed or item.chosen_store != store:
            continue
        items_to_add.append((item.product.store_product_id, item.quantity))

    if not items_to_add:
        return {"success": True, "message": f"No items to add to {store.value}", "count": 0}

    scraper = coles_scraper if store == Store.COLES else woolworths_scraper

    success = await scraper.add_to_cart(items_to_add)
    cart_url = await scraper.get_cart_url()

    if success:
        shopping_list.status = ListStatus.ORDERED
        await session.commit()

    return {
        "success": success,
        "count": len(items_to_add),
        "cart_url": cart_url,
        "message": (
            f"Added {len(items_to_add)} items to {store.value} cart"
            if success
            else f"Failed to add items to {store.value} cart"
        ),
    }
