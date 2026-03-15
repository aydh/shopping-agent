from pathlib import Path
from urllib.parse import quote

from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _product_image_url(url: str | None) -> str | None:
    """Route Coles images through the proxy; Woolworths CDN works directly."""
    if not url:
        return None
    if "coles.com.au" in url or "productimages.coles" in url:
        return f"/api/prices/image-proxy?url={quote(url, safe='')}"
    return url


templates.env.filters["product_image_url"] = _product_image_url
