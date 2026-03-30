import logging
import uuid
from datetime import date, datetime
from typing import TypedDict

from sqlalchemy import or_, select
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
    store_metrics: dict[str, "StoreMetrics"]
    single_store: Store | None
    coles_total: float
    woolworths_total: float
    best_total: float
    recommendation: str
    active_item_count: int
    total_quantity: int


class ShoppingListSummaryContext(TypedDict):
    """Summary-only context used for totals and small list metadata fragments."""

    shopping_list: ShoppingList | None
    store_metrics: dict[str, "StoreMetrics"]
    single_store: Store | None
    coles_total: float
    woolworths_total: float
    best_total: float
    recommendation: str
    active_item_count: int
    total_quantity: int


class StoreMetrics(TypedDict):
    """Per-store availability and matching summary for the shopping list."""

    available_count: int
    available_total: float
    unavailable_count: int
    unmatched_count: int
    matched_available_count: int
    matched_available_total: float


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


def _empty_store_metrics() -> dict[str, StoreMetrics]:
    """Return an empty per-store metrics structure."""
    return {
        "coles": {
            "available_count": 0,
            "available_total": 0.0,
            "unavailable_count": 0,
            "unmatched_count": 0,
            "matched_available_count": 0,
            "matched_available_total": 0.0,
        },
        "woolworths": {
            "available_count": 0,
            "available_total": 0.0,
            "unavailable_count": 0,
            "unmatched_count": 0,
            "matched_available_count": 0,
            "matched_available_total": 0.0,
        },
    }


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
    user_id: uuid.UUID,
    target_date: date | None = None,
    lookahead_days: int = 7,
) -> ShoppingList:
    """Generate a shopping list from consumption predictions.

    Loads all consumption predictions, generates candidates within a time
    window (lead_time_days before target_date to lookahead_days after),
    resolves prices via cross-store ProductMatch data, selects the best
    store per item based on price, and persists items to a DRAFT list.
    Auto-generated items from prior lists are cleared on regeneration.

    Args:
        session: Async database session for prediction/product/match queries.
        target_date: Target date for window calculation (defaults to today).
        lookahead_days: Days ahead of target_date to include in candidates.

    Returns:
        ShoppingList object (in DRAFT status) with ShoppingListItem children.

    Price and store selection logic:
        - Uses build_price_map() to look up coles_price and woolworths_price
          from ProductMatch records.
        - Falls back to product.current_price if no match exists (assumes
          product is from that store only).
        - Calls choose_best_store() to pick the cheaper store; ties go to
          the product's store.
    """
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
        select(ProductMatch)
        .options(
            selectinload(ProductMatch.product_a),
            selectinload(ProductMatch.product_b),
        )
        .where(ProductMatch.is_rejected == False)  # noqa: E712
    )
    price_map = build_price_map(list(matches_result.scalars().all()))

    # Create or update shopping list
    existing = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status == ListStatus.DRAFT, ShoppingList.user_id == user_id)
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
            user_id=user_id,
            name=list_name,
            target_date=target_date,
            status=ListStatus.DRAFT,
        )
        session.add(shopping_list)
        await session.flush()

    # Index predictions by product_id to avoid per-candidate lookups
    product_by_id = {p.product.id: p.product for p in predictions}

    # Add candidates
    for candidate in candidates:
        product = product_by_id.get(candidate.product_id)
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


async def get_active_list(session: AsyncSession, user_id: uuid.UUID) -> ShoppingList | None:
    """Return the most recent non-ordered shopping list with items eagerly loaded.

    Args:
        session: Async database session.
        user_id: UUID of the current user.

    Returns:
        The most recent ShoppingList not in ORDERED status, or None if none exist.
    """
    result = await session.execute(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status != ListStatus.ORDERED, ShoppingList.user_id == user_id)
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


async def assign_cheapest_stores(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Assign each active list item to its cheapest available store.

    Args:
        session: Async database session.
        user_id: UUID of the current user.

    Returns:
        Number of items whose store was assigned (0 if no active list).
    """
    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED, ShoppingList.user_id == user_id)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if not shopping_list:
        return 0

    items_result = await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == shopping_list.id,
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )
    items = items_result.scalars().all()
    for item in items:
        item.chosen_store = choose_best_store(
            item.coles_price, item.woolworths_price, item.chosen_store or Store.COLES
        )
    await session.commit()
    return len(items)


async def add_item_to_list(
    session: AsyncSession,
    user_id: uuid.UUID,
    product_id: int,
    quantity: int = 1,
) -> ShoppingListItem | None:
    """Add a product to the active shopping list.

    Handles partner resolution (finds cross-store price), deduplication
    (increments quantity if already on list), and IntegrityError (concurrent
    insert race condition).

    Args:
        session: Async database session.
        product_id: ID of the product to add.
        quantity: Quantity to add (default 1). If product already on list,
            increments by this amount.

    Returns:
        The ShoppingListItem added or updated, or None if no active list
        or product not found.
    """
    from sqlalchemy.exc import IntegrityError

    result = await session.execute(
        select(ShoppingList)
        .where(ShoppingList.status != ListStatus.ORDERED, ShoppingList.user_id == user_id)
        .order_by(ShoppingList.created_at.desc())
    )
    shopping_list = result.scalars().first()
    if not shopping_list:
        return None

    product = await session.get(Product, product_id)
    if not product:
        return None

    from .product_resolution import get_partner_product

    partner_store = "woolworths" if product.store == Store.COLES else "coles"
    partner_product = await get_partner_product(session, product_id, partner_store)
    partner_id = partner_product.id if partner_product else None

    candidate_ids = [product_id] + ([partner_id] if partner_id else [])
    existing = (await session.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.shopping_list_id == shopping_list.id,
            ShoppingListItem.product_id.in_(candidate_ids),
            ShoppingListItem.is_removed == False,  # noqa: E712
        )
    )).scalars().first()

    if existing:
        existing.quantity += quantity
        await session.commit()
        return existing

    coles_price = None
    woolworths_price = None
    chosen_store = product.store

    if partner_product:
        coles_p = product if product.store == Store.COLES else partner_product
        ww_p = product if product.store == Store.WOOLWORTHS else partner_product
        coles_price = coles_p.current_price if coles_p else None
        woolworths_price = ww_p.current_price if ww_p else None
        chosen_store = choose_best_store(coles_price, woolworths_price, product.store)
    else:
        if product.store == Store.COLES:
            coles_price = product.current_price
        else:
            woolworths_price = product.current_price

    try:
        item = ShoppingListItem(
            shopping_list_id=shopping_list.id,
            product_id=product_id,
            quantity=quantity,
            coles_price=coles_price,
            woolworths_price=woolworths_price,
            chosen_store=chosen_store,
            is_user_added=True,
        )
        session.add(item)
        await session.commit()
        return item
    except IntegrityError:
        await session.rollback()
        existing = (await session.execute(
            select(ShoppingListItem).where(
                ShoppingListItem.shopping_list_id == shopping_list.id,
                ShoppingListItem.product_id.in_(candidate_ids),
                ShoppingListItem.is_removed == False,  # noqa: E712
            )
        )).scalars().first()
        if existing:
            existing.quantity += quantity
            await session.commit()
        return existing


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

    active_items = [i for i in items if not i.is_removed]
    if not active_items:
        return display_names, store_names, store_products

    canonical_ids = [i.product.id for i in active_items]

    # Bulk-fetch all matches for these products in one query
    matches_result = await session.execute(
        select(ProductMatch).where(
            or_(
                ProductMatch.product_a_id.in_(canonical_ids),
                ProductMatch.product_b_id.in_(canonical_ids),
            ),
            ProductMatch.is_rejected == False,  # noqa: E712
        )
    )
    # Build: canonical product_id -> partner product_id
    partner_id_map: dict[int, int] = {}
    for m in matches_result.scalars().all():
        if m.product_a_id in canonical_ids and m.product_a_id not in partner_id_map:
            partner_id_map[m.product_a_id] = m.product_b_id
        if m.product_b_id in canonical_ids and m.product_b_id not in partner_id_map:
            partner_id_map[m.product_b_id] = m.product_a_id

    # Bulk-fetch all partner products in one query
    partner_ids = list(set(partner_id_map.values()) - set(canonical_ids))
    partners: dict[int, Product] = {}
    if partner_ids:
        partner_result = await session.execute(
            select(Product).where(Product.id.in_(partner_ids))
        )
        partners = {p.id: p for p in partner_result.scalars().all()}

    for item in active_items:
        canonical = item.product
        partner_id = partner_id_map.get(canonical.id)
        partner = partners.get(partner_id) if partner_id else None

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


def _build_shopping_list_summary(
    shopping_list: ShoppingList | None,
    store_products: dict[int, dict],
) -> ShoppingListSummaryContext:
    """Build totals and availability metrics for an active shopping list."""
    coles_total = 0.0
    woolworths_total = 0.0
    best_total = 0.0
    store_metrics = _empty_store_metrics()
    single_store: Store | None = None
    active_item_count = 0
    total_quantity = 0

    if shopping_list:
        active_items = [i for i in shopping_list.items if not i.is_removed]
        active_item_count = len(active_items)
        total_quantity = sum(item.quantity for item in active_items)
        stores_used = {i.chosen_store for i in active_items if i.chosen_store}
        single_store = stores_used.pop() if len(stores_used) == 1 else None

        for item in active_items:
            store_product_map = store_products.get(item.id, {})
            has_match = (
                store_product_map.get("coles") is not None
                and store_product_map.get("woolworths") is not None
            )
            matched_available_both = (
                has_match
                and item.coles_price is not None
                and item.woolworths_price is not None
            )
            for store_key, unit_price in (
                ("coles", item.coles_price),
                ("woolworths", item.woolworths_price),
            ):
                metrics = store_metrics[store_key]
                product = store_product_map.get(store_key)
                if product is None:
                    metrics["unmatched_count"] += 1
                    continue
                if unit_price is None:
                    metrics["unavailable_count"] += 1
                    continue

                line_total = unit_price * item.quantity
                metrics["available_count"] += 1
                metrics["available_total"] += line_total
                if matched_available_both:
                    metrics["matched_available_count"] += 1
                    metrics["matched_available_total"] += line_total

            cp = item.coles_price * item.quantity if item.coles_price is not None else None
            wp = item.woolworths_price * item.quantity if item.woolworths_price is not None else None
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
        "store_metrics": store_metrics,
        "single_store": single_store,
        "coles_total": coles_total,
        "woolworths_total": woolworths_total,
        "best_total": best_total,
        "recommendation": recommendation,
        "active_item_count": active_item_count,
        "total_quantity": total_quantity,
    }


async def get_shopping_list_summary_context(
    session: AsyncSession, user_id: uuid.UUID
) -> ShoppingListSummaryContext:
    """Build summary-only shopping list context for totals and counters."""
    shopping_list = await get_active_list(session, user_id)
    store_products: dict[int, dict] = {}
    if shopping_list:
        active_items = [i for i in shopping_list.items if not i.is_removed]
        if active_items:
            canonical_ids = [item.product.id for item in active_items]
            matched_ids: set[int] = set()
            matches_result = await session.execute(
                select(ProductMatch.product_a_id, ProductMatch.product_b_id).where(
                    or_(
                        ProductMatch.product_a_id.in_(canonical_ids),
                        ProductMatch.product_b_id.in_(canonical_ids),
                    ),
                    ProductMatch.is_rejected == False,  # noqa: E712
                )
            )
            for product_a_id, product_b_id in matches_result.all():
                if product_a_id in canonical_ids:
                    matched_ids.add(product_a_id)
                if product_b_id in canonical_ids:
                    matched_ids.add(product_b_id)

            for item in active_items:
                canonical = item.product
                has_partner = canonical.id in matched_ids
                if canonical.store == Store.COLES:
                    store_products[item.id] = {
                        "coles": canonical,
                        "woolworths": canonical if has_partner else None,
                    }
                else:
                    store_products[item.id] = {
                        "coles": canonical if has_partner else None,
                        "woolworths": canonical,
                    }
    return _build_shopping_list_summary(shopping_list, store_products)


async def get_shopping_list_context(session: AsyncSession, user_id: uuid.UUID) -> ShoppingListContext:
    """Build the full shopping list context dict for template rendering.

    Returns a dict with keys: shopping_list, display_names, store_names,
    store_products, store_metrics, single_store, coles_total,
    woolworths_total, best_total, recommendation.
    """
    query = (
        select(ShoppingList)
        .options(selectinload(ShoppingList.items).selectinload(ShoppingListItem.product))
        .where(ShoppingList.status != ListStatus.ORDERED, ShoppingList.user_id == user_id)
        .order_by(ShoppingList.created_at.desc())
    )
    result = await session.execute(query)
    shopping_list = result.scalars().first()
    display_names: dict[int, str] = {}
    store_names: dict[int, dict] = {}
    store_products: dict[int, dict] = {}
    if shopping_list:
        display_names, store_names, store_products = await resolve_display_names(session, shopping_list.items)
    summary = _build_shopping_list_summary(shopping_list, store_products)

    return {
        "shopping_list": shopping_list,
        "display_names": display_names,
        "store_names": store_names,
        "store_products": store_products,
        **summary,
    }


async def get_list_history(session: AsyncSession, user_id: uuid.UUID) -> list[ListHistoryRow]:
    """Return summary rows for past (ordered) shopping lists."""
    result = await session.execute(
        select(ShoppingList)
        .options(selectinload(ShoppingList.items))
        .where(ShoppingList.status == ListStatus.ORDERED, ShoppingList.user_id == user_id)
        .order_by(ShoppingList.created_at.desc())
    )
    rows: list[ListHistoryRow] = []
    for sl in result.scalars().all():
        active = [i for i in sl.items if not i.is_removed]
        stores = {i.chosen_store for i in active if i.chosen_store}
        store = stores.pop() if len(stores) == 1 else None
        total = 0.0
        for i in active:
            if i.chosen_store == Store.COLES:
                price = i.coles_price or 0
            elif i.chosen_store == Store.WOOLWORTHS:
                price = i.woolworths_price or 0
            else:
                price = 0
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
