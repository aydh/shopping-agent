"""Embedded MCP server for the shopping agent.

Exposes 19 tools for LLM agents to interact with grocery automation:
predictions, shopping lists, cart, order sync, price refresh, and product matching.

Mount: app.mount("/mcp", mcp.http_app()) in main.py
"""
import logging
import uuid
from typing import cast

from fastapi import HTTPException
from fastmcp import FastMCP

from ..config import settings
from ..database import async_session, set_rls_claims
from ..db_helpers import store_from_string
from ..models import ListStatus, Product, ProductMatch, ShoppingList, Store
from ..services.cart import add_to_cart
from ..services.order_sync import sync_orders as _sync_orders
from ..services.prediction import get_predictions_with_match_info, refresh_predictions as _refresh_predictions
from ..services.price_refresh import do_price_refresh
from ..services.shopping_list import (
    add_item_to_list,
    assign_cheapest_stores,
    confirm_list,
    generate_shopping_list,
    get_active_list,
    get_list_history,
    remove_item,
    resolve_display_names,
    update_item_quantity,
)
from ..services.price_comparison import compare_product_prices, find_or_create_match, match_unmatched_products
from ..scrapers.registry import get_scraper

logger = logging.getLogger(__name__)

mcp = FastMCP("shopping-agent")


def _get_mcp_user_id() -> uuid.UUID:
    """Return the configured MCP default user UUID.

    This is a stopgap until FastMCP OAuth 2.1 is configured. Set
    MCP_DEFAULT_USER_ID in the environment to the UUID of the user
    whose data MCP tools should operate on.
    """
    uid = settings.mcp_default_user_id
    if not uid:
        raise ValueError(
            "MCP_DEFAULT_USER_ID is not configured. "
            "Set it to a valid user UUID to enable MCP tools."
        )
    return uuid.UUID(uid)


def _scraper_for(store: str, user_id: uuid.UUID | None = None):
    """Return a scraper instance for the given store name."""
    s = store_from_string(store)
    if user_id:
        return get_scraper(user_id, s)
    # Fallback to global singleton (no user context)
    from ..scrapers.registry import coles_scraper, woolworths_scraper
    return coles_scraper if s == Store.COLES else woolworths_scraper


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_auth_status(store: str) -> dict:
    """Check whether valid session cookies are stored for a store.

    Args:
        store: Store name — "coles" or "woolworths".

    Returns:
        {"store": str, "authenticated": bool, "message": str}
    """
    try:
        user_id = _get_mcp_user_id()
        scraper = _scraper_for(store, user_id)
        authenticated = await scraper.is_authenticated()
        return {
            "store": store,
            "authenticated": authenticated,
            "message": "Connected" if authenticated else f"Not authenticated — import cookies for {store} first",
        }
    except (ValueError, HTTPException) as e:
        return {"store": store, "authenticated": False, "message": str(e)}


@mcp.tool()
async def get_predictions() -> list[dict]:
    """Get consumption predictions — what products are running low and when.

    Returns a list of predictions ordered by predicted runout date, with
    product name, store, confidence score, days until runout, and whether
    a cross-store price match exists.
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            predictions = await get_predictions_with_match_info(session, user_id)
    return [
            {
                "product_id": p.product_id,
                "product_name": cast(Product, p.product).name,
                "store": cast(Product, p.product).store.value,
                "predicted_runout_date": str(p.predicted_runout_date) if p.predicted_runout_date else None,
                "days_until_runout": p.days_until_runout,
                "confidence_score": round(p.confidence_score, 2),
                "last_purchased_date": str(p.last_purchased_date) if p.last_purchased_date else None,
                "last_purchase_store": p.last_purchase_store or None,
                "is_matched": p.is_matched,
                "matched_product_name": cast(Product, p.matched_product).name if p.matched_product else None,
            }
            for p in predictions
        ]


@mcp.tool()
async def get_shopping_list() -> dict:
    """Get the current active shopping list with items, prices, and store assignments.

    Returns the active DRAFT or CONFIRMED list with per-item details.
    If no active list exists, returns {"shopping_list": null}.
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            shopping_list = await get_active_list(session, user_id)
        if not shopping_list:
            return {"shopping_list": None, "items": []}
        active_items = [i for i in shopping_list.items if not i.is_removed]
        _, store_names, _ = await resolve_display_names(session, active_items)
        items = [
            {
                "item_id": item.id,
                "product_id": item.product_id,
                "quantity": item.quantity,
                "coles_name": store_names.get(item.id, {}).get("coles"),
                "woolworths_name": store_names.get(item.id, {}).get("woolworths"),
                "coles_price": item.coles_price,
                "woolworths_price": item.woolworths_price,
                "chosen_store": item.chosen_store.value if item.chosen_store else None,
                "is_user_added": item.is_user_added,
            }
            for item in active_items
        ]
        return {
            "list_id": shopping_list.id,
            "name": shopping_list.name,
            "status": shopping_list.status.value,
            "item_count": len(items),
            "items": items,
        }


@mcp.tool()
async def get_shopping_list_history() -> list[dict]:
    """Get past ORDERED shopping lists with summaries.

    Returns a list of completed lists ordered by most recent first.
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            history = await get_list_history(session, user_id)
    return [
        {
            "list_id": row["id"],
            "name": row["name"],
            "created_at": str(row["created_at"]),
            "status": row["status"].value,
            "store": row["store"].value if row["store"] else None,
            "item_count": row["item_count"],
            "total": row["total"],
        }
        for row in history
    ]


@mcp.tool()
async def search_products(query: str, store: str | None = None) -> list[dict]:
    """Search the Coles and/or Woolworths product catalog.

    Args:
        query: Product search query (e.g. "full cream milk 2L").
        store: Optional store filter — "coles" or "woolworths". Searches both if omitted.

    Returns:
        List of matching products with name, price, store, store_product_id.
        Requires valid cookies for the target store(s) — returns error entry if not authenticated.
    """
    results: list[dict] = []
    stores_to_search = []
    if store:
        try:
            stores_to_search = [store_from_string(store)]
        except (ValueError, HTTPException) as e:
            return [{"error": str(e)}]
    else:
        stores_to_search = [Store.COLES, Store.WOOLWORTHS]

    user_id = _get_mcp_user_id()
    for s in stores_to_search:
        scraper = _scraper_for(s.value, user_id)
        if not await scraper.is_authenticated():
            results.append({"store": s.value, "error": f"Not authenticated for {s.value}"})
            continue
        try:
            products = await scraper.search_product(query)
            for p in products:
                results.append({
                    "store": s.value,
                    "store_product_id": p.store_product_id,
                    "name": p.name,
                    "brand": p.brand,
                    "current_price": p.current_price,
                    "unit_size": p.unit_size,
                    "is_available": p.is_available,
                })
        except Exception as e:
            logger.warning("[MCP] search_products error for %s: %s", s.value, e)
            results.append({"store": s.value, "error": str(e)})

    return results


@mcp.tool()
async def get_price_comparison(product_id: int) -> dict:
    """Get Coles vs Woolworths price comparison for a product.

    Looks up the ProductMatch for the given product and returns both prices,
    the cheaper store, and the potential savings.

    Args:
        product_id: Database ID of the product.

    Returns:
        Price comparison with coles_price, woolworths_price, cheaper_store, savings.
        Returns error if product not found or no match exists.
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            comparisons = await compare_product_prices(session, [product_id])
    if not comparisons:
        return {"error": f"No price comparison found for product {product_id}"}
    c = comparisons[0]
    return {
        "product_name": c.product_name,
        "unit_size": c.unit_size,
        "coles_price": c.coles_price,
        "woolworths_price": c.woolworths_price,
        "cheaper_store": c.cheaper_store.value if c.cheaper_store else None,
        "savings": c.savings,
        "match_confidence": c.match_confidence,
        "is_confirmed": c.is_confirmed,
    }


# ---------------------------------------------------------------------------
# Shopping list management tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_shopping_list(from_predictions: bool = False) -> dict:
    """Create a new DRAFT shopping list.

    Fails if an active (non-ordered) list already exists — delete or close it first.

    Args:
        from_predictions: If True, pre-populate the list from consumption
            predictions (products predicted to run out within the lookahead window).
            If False, create an empty list.

    Returns:
        {"list_id": int, "item_count": int, "status": str} or {"error": str}
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            existing = await get_active_list(session, user_id)
            if existing:
                return {"error": f"An active shopping list already exists (id={existing.id}, status={existing.status.value}). Close or delete it first."}
            if from_predictions:
                shopping_list = await generate_shopping_list(session, user_id)
            else:
                shopping_list = ShoppingList(name="Shopping List", status=ListStatus.DRAFT, user_id=user_id)
                session.add(shopping_list)
                await session.flush()
        list_id = shopping_list.id
        item_count = sum(1 for item in shopping_list.items if not getattr(item, "is_removed", False))
        status = shopping_list.status.value
    return {"list_id": list_id, "item_count": item_count, "status": status}


@mcp.tool()
async def add_item_to_shopping_list(product_id: int, quantity: int = 1) -> dict:
    """Add a product to the active shopping list.

    If the product (or its cross-store match) is already on the list,
    the quantity is incremented instead of adding a duplicate.

    Args:
        product_id: Database ID of the product to add.
        quantity: Quantity to add (default 1, must be >= 1).

    Returns:
        {"item_id": int, "product_id": int, "quantity": int, "chosen_store": str | None, "status": str}
        or {"error": str} if no active list or product not found.
    """
    if quantity < 1:
        return {"error": "Quantity must be at least 1"}
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            item = await add_item_to_list(session, user_id, product_id=product_id, quantity=quantity)
        if item is None:
            return {"error": "No active shopping list or product not found"}
        return {
            "item_id": item.id,
            "product_id": item.product_id,
            "quantity": item.quantity,
            "chosen_store": item.chosen_store.value if item.chosen_store else None,
            "status": "added",
        }


@mcp.tool()
async def update_list_item_quantity(item_id: int, quantity: int) -> dict:
    """Update the quantity of an item on the active shopping list.

    Args:
        item_id: Database ID of the ShoppingListItem.
        quantity: New quantity (must be >= 1).

    Returns:
        {"item_id": int, "quantity": int} or {"error": str}
    """
    if quantity < 1:
        return {"error": "Quantity must be at least 1"}
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            await update_item_quantity(session, item_id, quantity)
    return {"item_id": item_id, "quantity": quantity}


@mcp.tool()
async def remove_list_item(item_id: int) -> dict:
    """Remove an item from the active shopping list (soft-delete).

    Args:
        item_id: Database ID of the ShoppingListItem to remove.

    Returns:
        {"item_id": int, "removed": bool}
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            removed = await remove_item(session, item_id)
    return {"item_id": item_id, "removed": removed}


@mcp.tool()
async def assign_cheapest_store_to_all() -> dict:
    """Assign each item on the active list to its cheapest available store.

    Uses current Coles and Woolworths prices to pick the cheaper option
    per item. Does NOT confirm the list — call confirm_shopping_list() after
    to proceed to cart.

    Returns:
        {"items_assigned": int} or {"error": str} if no active list.
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            count = await assign_cheapest_stores(session, user_id)
    if count == 0:
        return {"error": "No active list or no items to assign"}
    return {"items_assigned": count}


@mcp.tool()
async def confirm_shopping_list() -> dict:
    """Confirm the active shopping list, making it ready for cart addition.

    The list must be in DRAFT status. After confirming, use
    add_confirmed_list_to_cart() to add items to a store's cart.

    Returns:
        {"list_id": int, "status": str} or {"error": str}
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            shopping_list = await get_active_list(session, user_id)
            if not shopping_list:
                return {"error": "No active shopping list found"}
            confirmed = await confirm_list(session, shopping_list.id)
        if not confirmed:
            return {"error": "Failed to confirm list"}
        list_id = confirmed.id
        status = confirmed.status.value
    return {"list_id": list_id, "status": status}


@mcp.tool()
async def close_shopping_list() -> dict:
    """Mark the active shopping list as ORDERED, completing the workflow.

    The list must be CONFIRMED. Call this after add_confirmed_list_to_cart()
    succeeds so the list moves to history and a new list can be created.

    Returns:
        {"list_id": int, "status": "ordered"} or {"error": str}
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            shopping_list = await get_active_list(session, user_id)
            if not shopping_list:
                return {"error": "No active shopping list found"}
            if shopping_list.status != ListStatus.CONFIRMED:
                return {"error": f"List must be CONFIRMED before closing (current status: {shopping_list.status.value})"}
            shopping_list.status = ListStatus.ORDERED
            list_id = shopping_list.id
    return {"list_id": list_id, "status": "ordered"}


# ---------------------------------------------------------------------------
# Cart tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def add_confirmed_list_to_cart(store: str) -> dict:
    """Add all confirmed shopping list items for a store to its cart.

    The list must be CONFIRMED (call confirm_shopping_list() first).
    Items assigned to the specified store are resolved to store product IDs
    and added via the store's API.

    ⚠️  This action adds items to your real grocery cart.

    Args:
        store: Target store — "coles" or "woolworths".

    Returns:
        {"success": bool, "count": int, "cart_url": str, "message": str,
         "failed_item_ids": list[int]} or {"error": str}
    """
    try:
        store_enum = store_from_string(store)
    except (ValueError, HTTPException) as e:
        return {"error": str(e)}

    user_id = _get_mcp_user_id()
    coles_s = get_scraper(user_id, Store.COLES)
    ww_s = get_scraper(user_id, Store.WOOLWORTHS)
    target_scraper = coles_s if store_enum == Store.COLES else ww_s
    if not await target_scraper.is_authenticated():
        return {"error": f"Not authenticated for {store} — import cookies first"}

    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            result = await add_to_cart(session, store_enum, coles_s, ww_s)
    return dict(result)


# ---------------------------------------------------------------------------
# Data sync & refresh tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def sync_orders(store: str, limit: int | None = None) -> dict:
    """Fetch order history from a store and sync it to the local database.

    Streams orders from the store's API, upserts them, and creates product
    records for any new items discovered.

    ⚠️  This can be slow for large order histories (1-2 minutes).
    Use the `limit` parameter to cap the number of orders fetched.

    Args:
        store: Store to sync — "coles" or "woolworths".
        limit: Maximum number of orders to fetch (default: 200).

    Returns:
        {"store": str, "new_orders": int, "orders_fetched": int} or {"error": str}
    """
    try:
        store_enum = store_from_string(store)
    except (ValueError, HTTPException) as e:
        return {"error": str(e)}

    user_id = _get_mcp_user_id()
    scraper = _scraper_for(store, user_id)
    if not await scraper.is_authenticated():
        return {"error": f"Not authenticated for {store} — import cookies first"}

    try:
        fetch_limit = limit or 200
        scraped_orders = []
        async for order in scraper.stream_order_history(limit=fetch_limit):
            scraped_orders.append(order)

        async with async_session() as session:
            async with session.begin():
                await set_rls_claims(session, user_id)
                new_count = await _sync_orders(session, scraped_orders, store_enum, user_id)
    except Exception as e:
        logger.warning("[MCP] sync_orders failed for %s: %s", store, e)
        return {"error": f"Sync failed: {e}"}

    return {"store": store, "new_orders": new_count, "orders_fetched": len(scraped_orders)}


@mcp.tool()
async def refresh_prices(store: str) -> dict:
    """Refresh current prices for all products in a store.

    Fetches the latest price for every known product and updates the database.
    Also updates any active shopping list items with fresh prices.

    ⚠️  This can be slow (minutes for large product catalogs, especially
    Woolworths which has rate limiting). Requires valid store cookies.

    Args:
        store: Store to refresh — "coles" or "woolworths".

    Returns:
        {"store": str, "updated": int, "total": int} or {"error": str}
    """
    try:
        store_enum = store_from_string(store)
    except (ValueError, HTTPException) as e:
        return {"error": str(e)}

    user_id = _get_mcp_user_id()
    scraper = _scraper_for(store, user_id)
    if not await scraper.is_authenticated():
        return {"error": f"Not authenticated for {store} — import cookies first"}

    updated, total = await do_price_refresh(store_enum)
    return {"store": store, "updated": updated, "total": total}


@mcp.tool()
async def refresh_predictions() -> dict:
    """Recompute all consumption predictions from order history.

    Analyzes purchase intervals and quantities for each product and updates
    the predicted runout dates and confidence scores.

    Returns:
        {"predictions_updated": int}
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            count = await _refresh_predictions(session, user_id)
    return {"predictions_updated": count}


# ---------------------------------------------------------------------------
# Product matching tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def match_products(store: str | None = None) -> dict:
    """Auto-match unmatched products using fuzzy name matching.

    Finds unmatched products and attempts to pair them with their cross-store
    equivalent using rapidfuzz name similarity.

    Args:
        store: Store whose unmatched products to process — "coles" or
            "woolworths". If omitted, processes Coles only (one pass is
            sufficient since matches are bidirectional).

    Returns:
        {"matches_created": int, "by_store": dict} or {"error": str}
    """
    stores_to_process = []
    if store:
        try:
            stores_to_process = [store_from_string(store)]
        except (ValueError, HTTPException) as e:
            return {"error": str(e)}
    else:
        stores_to_process = [Store.COLES]

    user_id = _get_mcp_user_id()
    results: dict[str, int] = {}
    for s in stores_to_process:
        async with async_session() as session:
            async with session.begin():
                await set_rls_claims(session, user_id)
                count = await match_unmatched_products(session, s)
        results[s.value] = count

    total = sum(results.values())
    return {"matches_created": total, "by_store": results}


@mcp.tool()
async def find_product_match(product_id: int, query: str | None = None) -> dict:
    """Search for a cross-store match for a product.

    Looks for an existing match first, then falls back to local fuzzy matching.
    If a query is provided, also searches the opposite store's catalog
    (requires valid cookies for the target store).

    Args:
        product_id: Database ID of the product to find a match for.
        query: Optional search query to use for catalog search (uses product
            name if omitted).

    Returns:
        Match details if found, or {"match": null, "message": str} if not found.
        Returns {"error": str} if product not found.
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            product = await session.get(Product, product_id)
            if not product:
                return {"error": f"Product {product_id} not found"}

            target_store = Store.WOOLWORTHS if product.store == Store.COLES else Store.COLES
            target_scraper = get_scraper(user_id, target_store)

            scraper_to_use = None
            if await target_scraper.is_authenticated():
                scraper_to_use = target_scraper

            match = await find_or_create_match(session, product, target_store, scraper=scraper_to_use, search_query=query)
            if not match:
                return {"match": None, "message": "No match found"}

            partner_id = match.product_b_id if match.product_a_id == product_id else match.product_a_id
            partner = await session.get(Product, partner_id)

            result = {
                "match_id": match.id,
                "product_id": product_id,
                "partner_product_id": partner_id,
                "partner_name": partner.name if partner else None,
                "partner_store": partner.store.value if partner else None,
                "confidence": match.confidence,
                "match_method": match.match_method,
                "is_confirmed": match.is_confirmed,
                "is_rejected": match.is_rejected,
            }
    return result


@mcp.tool()
async def confirm_product_match(match_id: int) -> dict:
    """Mark a ProductMatch as confirmed (human-verified correct).

    Args:
        match_id: Database ID of the ProductMatch to confirm.

    Returns:
        {"match_id": int, "confirmed": bool} or {"error": str}
    """
    user_id = _get_mcp_user_id()
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user_id)
            match = await session.get(ProductMatch, match_id)
            if not match:
                return {"error": f"ProductMatch {match_id} not found"}
            match.is_confirmed = True
            match.is_rejected = False
    return {"match_id": match_id, "confirmed": True}
