from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi.templating import Jinja2Templates

from .config import APP_TIMEZONE

if TYPE_CHECKING:
    from .auth import CurrentUser

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _product_image_url(url: str | None) -> str | None:
    """Route Coles images through the proxy; Woolworths CDN works directly."""
    if not url:
        return None
    if "productimages.coles.com.au" in url:
        return f"/api/prices/image-proxy?url={quote(url, safe='')}"
    return url


def _product_url(
    url: str | None,
    store_product_id: str | None = None,
    store: str | None = None,
    name: str | None = None,
) -> str | None:
    """Return a full product URL, constructing one from store/id/name if not stored."""
    if url and url.startswith("http"):
        return url
    # Legacy Woolworths slug stored without base URL
    if url and store_product_id:
        return f"https://www.woolworths.com.au/shop/productdetails/{store_product_id}/{url}"
    # Construct URL from known store patterns when product_url was never stored
    if store == "coles" and store_product_id and name:
        slug = name.lower().replace(" ", "-")
        return f"https://www.coles.com.au/product/{slug}-{store_product_id}"
    if store == "woolworths" and store_product_id:
        return f"https://www.woolworths.com.au/shop/productdetails/{store_product_id}"
    return None


def _localtime(dt: datetime) -> datetime:
    """Convert a UTC (or naive-UTC) datetime to APP_TIMEZONE for display."""
    if dt is None:
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(APP_TIMEZONE)


def _get_nav_user(request) -> "CurrentUser | None":
    """Return the current user for nav rendering, or None if not authenticated.

    Uses the same token cache as the auth layer so this is cheap on repeat calls.
    Import is local to avoid a circular import at module load time.
    """
    from .auth import CurrentUser, _claims_to_user, _decode_token  # noqa: F401

    token = request.cookies.get("sb-access-token")
    if not token:
        return None
    try:
        claims = _decode_token(token)
        return _claims_to_user(claims)
    except Exception:
        return None


templates.env.filters["product_image_url"] = _product_image_url
templates.env.filters["product_url"] = _product_url
templates.env.filters["localtime"] = _localtime
templates.env.globals["get_nav_user"] = _get_nav_user
