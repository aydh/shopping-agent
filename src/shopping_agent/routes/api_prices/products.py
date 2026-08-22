"""Product visibility management and image proxy."""
import logging
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ...cache import image_cache
from ...log_utils import scrub
from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import get_user_session_from_cookie
from ...db_helpers import store_from_string
from ...models import ConsumptionPrediction, PriceHistory, Product, ProductMatch, UserProductPreferences

router = APIRouter()
logger = logging.getLogger(__name__)

_PROXY_HEADERS = {
    "Referer": "https://www.coles.com.au/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# 7 days — product images rarely change; browser will serve from cache without a network request
_IMAGE_CACHE_CONTROL = "public, max-age=604800, immutable"

# Hosts the image proxy is permitted to fetch from. The proxy makes a
# server-side request to whatever URL it is given, so without this allowlist it
# is a Server-Side Request Forgery (SSRF) primitive: an authenticated user could
# point it at cloud metadata endpoints (e.g. 169.254.169.254), internal-only
# services, or localhost and read the response. Only the grocery image CDNs the
# app actually links to are permitted.
_ALLOWED_IMAGE_HOSTS = frozenset({
    "productimages.coles.com.au",
    "cdn.productimages.coles.com.au",
    "cdn0.woolworths.media",
})


async def _matched_product_ids(session: AsyncSession, product_id: int) -> set[int]:
    """Return all products connected by active matches, including the source product."""
    connected_ids = {product_id}
    frontier = {product_id}

    while frontier:
        rows = await session.execute(
            select(ProductMatch.product_a_id, ProductMatch.product_b_id).where(
                ProductMatch.is_rejected == False,  # noqa: E712
                or_(
                    ProductMatch.product_a_id.in_(frontier),
                    ProductMatch.product_b_id.in_(frontier),
                ),
            )
        )
        next_frontier: set[int] = set()
        for a_id, b_id in rows.all():
            for candidate_id in (a_id, b_id):
                if candidate_id not in connected_ids:
                    connected_ids.add(candidate_id)
                    next_frontier.add(candidate_id)
        frontier = next_frontier

    return connected_ids


@router.get("/image-proxy")
async def image_proxy(url: str) -> Response:
    """Proxy product images to bypass CDN hotlink protection.

    ``url`` is user-supplied and drives a server-side request, so it is
    validated against an https + host allowlist here, before any fetch, to
    prevent Server-Side Request Forgery (CWE-918).
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_IMAGE_HOSTS:
        raise HTTPException(status_code=400, detail="URL host not allowed")

    cached = await image_cache.get(url)
    if cached is not None:
        logger.debug("Image cache hit: %s", scrub(url))
        content, media_type = cached
        return Response(content=content, media_type=media_type, headers={"Cache-Control": _IMAGE_CACHE_CONTROL})

    logger.debug("Image cache miss: %s", scrub(url))
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_PROXY_HEADERS)
            if resp.status_code == 200:
                media_type = resp.headers.get("content-type", "image/jpeg")
                await image_cache.set(url, resp.content, media_type)
                return Response(content=resp.content, media_type=media_type, headers={"Cache-Control": _IMAGE_CACHE_CONTROL})
            raise httpx.HTTPStatusError(
                f"Upstream returned {resp.status_code}", request=resp.request, response=resp
            )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Image not found")
        logger.warning("Image proxy upstream error: %d", exc.response.status_code)
        raise HTTPException(status_code=502, detail="Upstream error fetching image")
    except httpx.RequestError:
        logger.exception("Network error fetching image")
        raise HTTPException(status_code=502, detail="Network error fetching image")


@router.post("/product/{product_id}/hide")
async def hide_product(product_id: int, user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie)) -> HTMLResponse:
    """Mark a product as hidden for this user (no longer buying). Removes their prediction."""
    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse("")

    connected_ids = await _matched_product_ids(session, product_id)
    await session.execute(
        pg_insert(UserProductPreferences)
        .values([{"user_id": user.user_id, "product_id": pid, "is_hidden": True} for pid in connected_ids])
        .on_conflict_do_update(
            index_elements=["user_id", "product_id"],
            set_={"is_hidden": True},
        )
    )
    await session.execute(
        delete(ConsumptionPrediction).where(ConsumptionPrediction.product_id.in_(connected_ids))
    )
    await session.commit()
    return HTMLResponse("")


@router.post("/product/{product_id}/restore")
async def restore_product(product_id: int, user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie)) -> HTMLResponse:
    """Restore a hidden product for this user."""
    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse("")

    connected_ids = await _matched_product_ids(session, product_id)
    await session.execute(
        update(UserProductPreferences)
        .where(
            UserProductPreferences.user_id == user.user_id,
            UserProductPreferences.product_id.in_(connected_ids),
        )
        .values(is_hidden=False)
    )
    await session.commit()
    return HTMLResponse("")


@router.delete("/products/purge/{store}")
async def purge_products(store: str, user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie)) -> HTMLResponse:
    store_enum = store_from_string(store)
    product_subq = select(Product.id).where(Product.store == store_enum).scalar_subquery()
    await session.execute(delete(ProductMatch).where(
        ProductMatch.product_a_id.in_(product_subq) | ProductMatch.product_b_id.in_(product_subq)
    ))
    await session.execute(delete(PriceHistory).where(PriceHistory.product_id.in_(product_subq)))
    await session.execute(delete(ConsumptionPrediction).where(ConsumptionPrediction.product_id.in_(product_subq)))
    result = await session.execute(delete(Product).where(Product.store == store_enum))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} {store_enum.value} products.</span>'  # type: ignore[attr-defined]
    )
