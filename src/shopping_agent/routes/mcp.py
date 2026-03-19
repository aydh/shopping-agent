"""Embedded MCP server for the shopping agent.

Exposes 19 tools for LLM agents to interact with grocery automation:
predictions, shopping lists, cart, order sync, price refresh, and product matching.

Mount: app.mount("/mcp", mcp.http_app()) in main.py
"""
import logging

from fastmcp import FastMCP

from ..database import async_session
from ..db_helpers import store_from_string
from ..models import ListStatus, Product, ProductMatch, ShoppingList, Store
from ..services.prediction import get_predictions_with_match_info
from ..services.prediction import refresh_predictions as _refresh_predictions
from ..services.shopping_list import (
    add_item_to_list,
    assign_cheapest_stores,
    confirm_list,
    generate_shopping_list,
    get_active_list,
    get_list_history,
    get_shopping_list_context,
    remove_item,
    update_item_quantity,
)
from ..services.cart import add_to_cart
from ..services.price_comparison import (
    compare_product_prices,
    find_or_create_match,
    match_unmatched_products,
)
from ..services.price_refresh import do_price_refresh
from ..services.order_sync import sync_orders as _sync_orders
from ..scrapers.coles import coles_scraper
from ..scrapers.woolworths import woolworths_scraper

logger = logging.getLogger(__name__)

mcp = FastMCP("shopping-agent")


def _scraper_for(store: str):
    """Return the scraper instance for the given store name."""
    s = store_from_string(store)
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
        scraper = _scraper_for(store)
        authenticated = await scraper.is_authenticated()
        return {
            "store": store,
            "authenticated": authenticated,
            "message": "Connected" if authenticated else f"Not authenticated — import cookies for {store} first",
        }
    except ValueError as e:
        return {"store": store, "authenticated": False, "message": str(e)}


@mcp.tool()
async def get_predictions() -> list[dict]:
    """Get consumption predictions — what products are running low and when.

    Returns a list of predictions ordered by predicted runout date, with
    product name, store, confidence score, days until runout, and whether
    a cross-store price match exists.
    """
    async with async_session() as session:
        predictions = await get_predictions_with_match_info(session)
    return [
        {
            "product_id": p.product_id,
            "product_name": p.product.name,
            "store": p.product.store.value,
            "predicted_runout_date": str(p.predicted_runout_date) if p.predicted_runout_date else None,
            "days_until_runout": p.days_until_runout,
            "confidence_score": round(p.confidence_score, 2),
            "last_purchased_date": str(p.last_purchased_date) if p.last_purchased_date else None,
            "last_purchase_store": p.last_purchase_store.value if p.last_purchase_store else None,
            "is_matched": p.is_matched,
            "matched_product_name": p.matched_product.name if p.matched_product else None,
        }
        for p in predictions
    ]


@mcp.tool()
async def get_shopping_list() -> dict:
    """Get the current active shopping list with items, prices, and store assignments.

    Returns the active DRAFT or CONFIRMED list with per-item details.
    If no active list exists, returns {"shopping_list": null}.
    """
    async with async_session() as session:
        shopping_list = await get_active_list(session)
        if not shopping_list:
            return {"shopping_list": None, "items": []}
        items = [
            {
                "item_id": item.id,
                "product_id": item.product_id,
                "quantity": item.quantity,
                "coles_price": item.coles_price,
                "woolworths_price": item.woolworths_price,
                "chosen_store": item.chosen_store.value if item.chosen_store else None,
                "is_user_added": item.is_user_added,
            }
            for item in shopping_list.items
            if not item.is_removed
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
    async with async_session() as session:
        history = await get_list_history(session)
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
    results = []
    stores_to_search = []
    if store:
        try:
            stores_to_search = [store_from_string(store)]
        except ValueError as e:
            return [{"error": str(e)}]
    else:
        stores_to_search = [Store.COLES, Store.WOOLWORTHS]

    for s in stores_to_search:
        scraper = coles_scraper if s == Store.COLES else woolworths_scraper
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
    async with async_session() as session:
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
