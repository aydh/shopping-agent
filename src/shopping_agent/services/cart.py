import logging
from typing import TypedDict

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import ListStatus, Product, ProductMatch, ShoppingList, ShoppingListItem, Store


class CartResult(TypedDict, total=False):
    """Result dict returned by add_to_cart.

    All keys except ``success`` are optional — error responses only include
    ``success`` and ``error``, while success responses include ``count``,
    ``cart_url``, ``message``, and ``failed_item_ids``.
    """

    success: bool
    error: str
    count: int
    cart_url: str | None
    message: str
    failed_item_ids: list[int]

logger = logging.getLogger(__name__)


def _resolve_store_product_id(
    canonical_product: Product, store: Store, partner_map: dict[int, Product]
) -> str | None:
    """Return the store_product_id for the given store.

    If the canonical product belongs to the target store, return it directly.
    Otherwise look up the partner product from the pre-loaded partner_map.
    """
    if canonical_product.store == store:
        return canonical_product.store_product_id

    partner = partner_map.get(canonical_product.id)
    if partner and partner.store == store:
        return partner.store_product_id

    return None


async def add_to_cart(session: AsyncSession, store: Store, coles_scraper, woolworths_scraper) -> CartResult:
    """Add confirmed shopping list items to the specified store's cart.

    Retrieves the most recent CONFIRMED shopping list, resolves each item's
    store-specific product ID (using cross-store ProductMatch if needed),
    calls the appropriate scraper to add items, and updates item.is_ordered
    flags in the database based on per-item success/failure results.

    Args:
        session: Async database session for product lookups and updates.
        store: Target store (COLES or WOOLWORTHS).

    Returns:
        CartResult TypedDict with:
        - success (bool): True if no items failed and no products were skipped.
        - count (int): Number of items successfully added to cart.
        - cart_url (str | None): URL to the store's cart page.
        - message (str): Summary message including count, store name, and
            count of skipped items (those with no product match).
        - failed_item_ids (list[int]): IDs of items that failed to add
            (not including those skipped due to no product match).
        - error (str, optional): Error message if no confirmed list exists.

    DB mutations:
        - Sets item.is_ordered = True for successfully added items.
        - Commits changes to the session.
    """
    result = await session.execute(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status == ListStatus.CONFIRMED)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()

    if not shopping_list:
        return {"success": False, "error": "No confirmed shopping list found"}

    # Bulk-load all ProductMatch records to avoid N+1 queries
    product_ids = [item.product.id for item in shopping_list.items]
    match_rows = await session.execute(
        select(ProductMatch)
        .options(selectinload(ProductMatch.product_a), selectinload(ProductMatch.product_b))
        .where(
            or_(
                ProductMatch.product_a_id.in_(product_ids),
                ProductMatch.product_b_id.in_(product_ids),
            ),
            ProductMatch.is_rejected == False,  # noqa: E712
        )
    )
    # Build map of product_id -> partner_product for O(1) lookups
    partner_map: dict[int, Product] = {}
    for m in match_rows.scalars():
        if m.product_a_id in product_ids:
            partner_map[m.product_a_id] = m.product_b
        if m.product_b_id in product_ids:
            partner_map[m.product_b_id] = m.product_a

    # Collect items for this store, resolving the correct store_product_id for each
    items_to_add: list[tuple[str, int]] = []
    spid_to_item_id: dict[str, int] = {}
    skipped_names: list[str] = []

    for item in shopping_list.items:
        if item.is_removed or item.is_ordered or item.chosen_store != store:
            continue
        store_product_id = _resolve_store_product_id(item.product, store, partner_map)
        logger.info(
            "Cart resolve: item=%s canonical=%s(%s/%s) -> %s_pid=%s",
            item.id,
            item.product.name,
            item.product.store.value,
            item.product.store_product_id,
            store.value,
            store_product_id,
        )
        if not store_product_id:
            logger.warning(
                "Could not resolve %s product ID for item %s (%s), skipping",
                store.value,
                item.id,
                item.product.name,
            )
            skipped_names.append(item.product.name)
            continue
        items_to_add.append((store_product_id, item.quantity))
        spid_to_item_id[str(store_product_id)] = item.id

    if not items_to_add:
        msg = f"No items to add to {store.value} cart"
        if skipped_names:
            msg += f" ({len(skipped_names)} items had no {store.value} product match)"
        return {"success": True, "message": msg, "count": 0, "failed_item_ids": []}

    scraper = coles_scraper if store == Store.COLES else woolworths_scraper

    results = await scraper.add_to_cart(items_to_add)
    cart_url = await scraper.get_cart_url()

    # Mark individual items as ordered based on per-item results
    failed_item_ids: list[int] = []
    succeeded = 0
    try:
        for spid, success in results.items():
            item_id = spid_to_item_id.get(spid)
            if item_id:
                db_item: ShoppingListItem | None = await session.get(ShoppingListItem, item_id)
                if db_item is not None:
                    if success:
                        db_item.is_ordered = True
                        succeeded += 1
                    else:
                        failed_item_ids.append(item_id)

        await session.commit()
    except Exception as e:
        logger.error(
            "Failed to mark items as ordered after cart add for store %s: %s",
            store.value,
            e,
            exc_info=True,
        )
        await session.rollback()
        return {
            "success": False,
            "error": "Items were added to cart but failed to update order status in database",
        }

    # Also count items skipped due to no product match as failed
    # (they won't be in results, but we should report them)

    overall_success = len(failed_item_ids) == 0 and not skipped_names
    msg = f"Added {succeeded}/{len(items_to_add)} items to {store.value} cart"
    if skipped_names:
        msg += f" ({len(skipped_names)} items had no {store.value} product match)"
    return {
        "success": overall_success,
        "count": succeeded,
        "cart_url": cart_url,
        "message": msg,
        "failed_item_ids": failed_item_ids,
    }
