import logging
from datetime import date, datetime
from typing import TypedDict

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


class ShoppingListContext(TypedDict):
    """Full context dict returned by get_shopping_list_context for template rendering."""

    shopping_list: ShoppingList | None
    display_names: dict[int, str]
    store_names: dict[int, dict]
    store_products: dict[int, dict]
    single_store: Store | None
    coles_total: float
    woolworths_total: float
    best_total: float
    recommendation: str


class ListHistoryRow(TypedDict):
    """Summary row for a single past shopping list returned by get_list_history."""

    id: int
    name: str
    created_at: datetime
    status: ListStatus
    store: Store | None
    item_count: int
    total: float

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
    """Update the quantity of a shopping list item, or mark it removed if quantity <= 0.

    Args:
        session: Async database session.
        item_id: ID of the ShoppingListItem to update.
        quantity: New quantity; values <= 0 mark the item as removed.

    Returns:
        The updated ShoppingListItem, or None if not found.
    """
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
    """Change the chosen store for a shopping list item.

    Args:
        session: Async database session.
        item_id: ID of the ShoppingListItem to update.
        store: The store to assign as the chosen store for this item.

    Returns:
        The updated ShoppingListItem, or None if not found.
    """
    item = await session.get(ShoppingListItem, item_id)
    if item:
        item.chosen_store = store
        await session.commit()
    return item


async def remove_item(session: AsyncSession, item_id: int) -> bool:
    """Soft-delete a shopping list item by marking it as removed.

    Args:
        session: Async database session.
        item_id: ID of the ShoppingListItem to remove.

    Returns:
        True if the item was found and marked removed, False otherwise.
    """
    item = await session.get(ShoppingListItem, item_id)
    if item:
        item.is_removed = True
        await session.commit()
        return True
    return False


async def get_active_list(session: AsyncSession) -> ShoppingList | None:
    """Return the most recent non-ordered shopping list with items eagerly loaded.

    Args:
        session: Async database session.

    Returns:
        The most recent ShoppingList not in ORDERED status, or None if none exist.
    """
    result = await session.execute(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    return result.scalars().first()


async def confirm_list(session: AsyncSession, list_id: int) -> ShoppingList | None:
    """Transition a shopping list to CONFIRMED status.

    Args:
        session: Async database session.
        list_id: ID of the ShoppingList to confirm.

    Returns:
        The confirmed ShoppingList, or None if not found.
    """
    shopping_list = await session.get(ShoppingList, list_id)
    if shopping_list:
        shopping_list.status = ListStatus.CONFIRMED
        await session.commit()
    return shopping_list


async def resolve_display_names(
    session: AsyncSession, items: list[ShoppingListItem]
) -> tuple[dict[int, str], dict[int, dict], dict[int, dict]]:
    """Resolve per-item display names and per-store product mappings.

    For each non-removed item, looks up the cross-store match to determine
    which store names and products are available.

    Args:
        session: Async database session.
        items: Shopping list items to resolve.

    Returns:
        Tuple of (display_names, store_names, store_products) where each is
        keyed by item.id:
        - display_names: the chosen store's product name (fallback: canonical name)
        - store_names: {'coles': name|None, 'woolworths': name|None}
        - store_products: {'coles': Product|None, 'woolworths': Product|None}
    """
    display_names: dict[int, str] = {}
    store_names: dict[int, dict] = {}
    store_products: dict[int, dict] = {}
    for item in items:
        if item.is_removed:
            continue
        canonical = item.product
        partner = None
        match_result = await session.execute(
            select(ProductMatch).where(
                (
                    (ProductMatch.product_a_id == canonical.id)
                    | (ProductMatch.product_b_id == canonical.id)
                ),
                ProductMatch.is_rejected == False,  # noqa: E712
            )
        )
        match = match_result.scalars().first()
        if match:
            partner_id = (
                match.product_b_id if match.product_a_id == canonical.id else match.product_a_id
            )
            partner = await session.get(Product, partner_id)

        if canonical.store == Store.COLES:
            coles_product = canonical
            ww_product = partner if partner and partner.store == Store.WOOLWORTHS else None
        else:
            ww_product = canonical
            coles_product = partner if partner and partner.store == Store.COLES else None

        coles_name = coles_product.name if coles_product else None
        woolworths_name = ww_product.name if ww_product else None

        store_names[item.id] = {"coles": coles_name, "woolworths": woolworths_name}
        store_products[item.id] = {"coles": coles_product, "woolworths": ww_product}

        if item.chosen_store == Store.COLES and coles_name:
            display_names[item.id] = coles_name
        elif item.chosen_store == Store.WOOLWORTHS and woolworths_name:
            display_names[item.id] = woolworths_name
        else:
            display_names[item.id] = canonical.name

    return display_names, store_names, store_products


async def get_shopping_list_context(session: AsyncSession) -> ShoppingListContext:
    """Build the full shopping list context dict for template rendering.

    Returns a dict with keys: shopping_list, display_names, store_names,
    store_products, single_store, coles_total, woolworths_total, best_total,
    recommendation.
    """
    query = (
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status != ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    result = await session.execute(query)
    shopping_list = result.scalars().first()

    coles_total = 0.0
    woolworths_total = 0.0
    best_total = 0.0
    display_names: dict[int, str] = {}
    store_names: dict[int, dict] = {}
    store_products: dict[int, dict] = {}
    single_store: Store | None = None
    if shopping_list:
        display_names, store_names, store_products = await resolve_display_names(session, shopping_list.items)
        active_items = [i for i in shopping_list.items if not i.is_removed]
        stores_used = {i.chosen_store for i in active_items if i.chosen_store}
        single_store = stores_used.pop() if len(stores_used) == 1 else None
        for item in active_items:
            cp = item.coles_price * item.quantity if item.coles_price else None
            wp = item.woolworths_price * item.quantity if item.woolworths_price else None
            # Each store total uses that store's price where available, falls back to
            # the other store so all three totals always represent the full basket.
            coles_total += cp if cp is not None else (wp or 0)
            woolworths_total += wp if wp is not None else (cp or 0)
            best_total += min(cp, wp) if cp is not None and wp is not None else (cp or wp or 0)

    recommendation = ""
    if coles_total and woolworths_total:
        if coles_total < woolworths_total:
            recommendation = f"Coles is ${woolworths_total - coles_total:.2f} cheaper overall"
        elif woolworths_total < coles_total:
            recommendation = f"Woolworths is ${coles_total - woolworths_total:.2f} cheaper overall"
        else:
            recommendation = "Same price at both stores"

    return {
        "shopping_list": shopping_list,
        "display_names": display_names,
        "store_names": store_names,
        "store_products": store_products,
        "single_store": single_store,
        "coles_total": coles_total,
        "woolworths_total": woolworths_total,
        "best_total": best_total,
        "recommendation": recommendation,
    }


async def get_list_history(session: AsyncSession) -> list[ListHistoryRow]:
    """Return summary rows for past (ordered) shopping lists."""
    result = await session.execute(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items))
        .where(ShoppingList.status == ListStatus.ORDERED)
        .order_by(ShoppingList.created_at.desc())
    )
    rows: list[ListHistoryRow] = []
    for sl in result.scalars().all():
        active = [i for i in sl.items if not i.is_removed]
        stores = {i.chosen_store for i in active if i.chosen_store}
        store = stores.pop() if len(stores) == 1 else None
        total = 0.0
        for i in active:
            price = (i.coles_price if store == Store.COLES else i.woolworths_price) or 0
            total += price * i.quantity
        rows.append({
            "id": sl.id,
            "name": sl.name,
            "created_at": sl.created_at,
            "status": sl.status,
            "store": store,
            "item_count": len(active),
            "total": total,
        })
    return rows
