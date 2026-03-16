from pathlib import Path
from urllib.parse import quote

from fastapi.templating import Jinja2Templates

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


templates.env.filters["product_image_url"] = _product_image_url
templates.env.filters["product_url"] = _product_url
