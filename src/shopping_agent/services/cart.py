import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import ListStatus, Product, ProductMatch, ShoppingList, ShoppingListItem, Store
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper

logger = logging.getLogger(__name__)


async def _resolve_store_product_id(
    session: AsyncSession, canonical_product: Product, store: Store
) -> str | None:
    """Return the store_product_id for the given store.

    If the canonical product belongs to the target store, return it directly.
    Otherwise look up the ProductMatch to find the partner product.
    """
    if canonical_product.store == store:
        return canonical_product.store_product_id

    result = await session.execute(
        select(ProductMatch).where(
            (
                (ProductMatch.product_a_id == canonical_product.id)
                | (ProductMatch.product_b_id == canonical_product.id)
            ),
            ProductMatch.is_rejected == False,  # noqa: E712
        )
    )
    match = result.scalars().first()
    if not match:
        return None

    partner_id = (
        match.product_b_id if match.product_a_id == canonical_product.id else match.product_a_id
    )
    partner = await session.get(Product, partner_id)
    if partner and partner.store == store:
        return partner.store_product_id

    return None


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

    # Collect items for this store, resolving the correct store_product_id for each
    items_to_add: list[tuple[str, int]] = []
    spid_to_item_id: dict[str, int] = {}
    skipped_names: list[str] = []

    for item in shopping_list.items:
        if item.is_removed or item.chosen_store != store:
            continue
        store_product_id = await _resolve_store_product_id(session, item.product, store)
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
    for spid, success in results.items():
        item_id = spid_to_item_id.get(spid)
        if item_id:
            item = await session.get(ShoppingListItem, item_id)
            if item:
                if success:
                    item.is_ordered = True
                    succeeded += 1
                else:
                    failed_item_ids.append(item_id)

    # Also count items skipped due to no product match as failed
    # (they won't be in results, but we should report them)

    await session.commit()

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
