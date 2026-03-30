"""Settings page view."""
import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import get_user_session_from_cookie
from ...models import Store
from ...scrapers.registry import get_scraper
from ...services.data_management import get_db_counts
from ...templating import templates

router = APIRouter()


def _counts_rows(counts: dict) -> list[dict]:
    """Build table row data for the settings data-management section."""
    return [
        {"label": "Coles Orders", "count": f"{counts['coles_orders']} orders, {counts['coles_order_items']} items", "also_deletes": "price history", "endpoint": "/api/orders/purge/coles", "tid": "purge-coles"},
        {"label": "Woolworths Orders", "count": f"{counts['woolworths_orders']} orders, {counts['woolworths_order_items']} items", "also_deletes": "price history", "endpoint": "/api/orders/purge/woolworths", "tid": "purge-woolworths"},
        {"label": "Coles Products", "count": str(counts["coles_products"]), "also_deletes": "matches, price history, predictions", "endpoint": "/api/prices/products/purge/coles", "tid": "purge-coles-products"},
        {"label": "Woolworths Products", "count": str(counts["woolworths_products"]), "also_deletes": "matches, price history, predictions", "endpoint": "/api/prices/products/purge/woolworths", "tid": "purge-woolworths-products"},
        {"label": "Product Matches", "count": str(counts["product_matches"]), "also_deletes": "\u2014", "endpoint": "/api/prices/matches/purge", "tid": "purge-matches"},
        {"label": "Price History", "count": str(counts["price_history"]), "also_deletes": "\u2014", "endpoint": "/api/prices/history/purge", "tid": "purge-price-history"},
        {"label": "Predictions", "count": str(counts["predictions"]), "also_deletes": "\u2014", "endpoint": "/api/predictions/purge", "tid": "purge-predictions"},
        {"label": "Shopping Lists", "count": f"{counts['shopping_lists']} lists, {counts['shopping_list_items']} items", "also_deletes": "\u2014", "endpoint": "/api/shopping-list/purge", "tid": "purge-lists"},
    ]


@router.get("/api/settings/counts")
async def settings_counts(
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Return HTML fragment of data-management counts (polled by HTMX)."""
    counts = await get_db_counts(session)
    html = templates.env.get_template("_settings_counts.html").render(rows=_counts_rows(counts))
    return HTMLResponse(html)


@router.get("/settings")
async def settings_page(
    request: Request,
    user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie),
) -> HTMLResponse:
    """Render the settings page."""
    coles_scraper = get_scraper(user.user_id, Store.COLES)
    woolworths_scraper = get_scraper(user.user_id, Store.WOOLWORTHS)
    coles_connected, woolworths_connected = await asyncio.gather(
        coles_scraper.is_authenticated(),
        woolworths_scraper.is_authenticated(),
    )
    counts = await get_db_counts(session)
    counts_rows_html = templates.env.get_template("_settings_counts.html").render(
        rows=_counts_rows(counts)
    )

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active_page": "settings",
            "coles_connected": coles_connected,
            "woolworths_connected": woolworths_connected,
            "counts": counts,
            "counts_rows_html": counts_rows_html,
        },
    )
