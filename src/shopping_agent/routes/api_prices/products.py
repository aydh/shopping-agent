"""Product visibility management and image proxy."""
import logging

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...models import ConsumptionPrediction, PriceHistory, Product, ProductMatch, Store

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/image-proxy")
async def image_proxy(url: str) -> Response:
    """Proxy product images to bypass CDN hotlink protection."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={
                "Referer": "https://www.coles.com.au/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            })
            if resp.status_code == 200:
                return StreamingResponse(iter([resp.content]), media_type=resp.headers.get("content-type", "image/jpeg"))
    except Exception:
        pass
    return Response(status_code=404)


@router.post("/product/{product_id}/hide")
async def hide_product(product_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Mark a product as hidden (no longer buying). Removes its prediction."""
    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse("")
    product.is_hidden = True
    pred_result = await session.execute(
        select(ConsumptionPrediction).where(ConsumptionPrediction.product_id == product_id)
    )
    pred = pred_result.scalar_one_or_none()
    if pred:
        await session.delete(pred)
    await session.commit()
    return HTMLResponse("")


@router.post("/product/{product_id}/restore")
async def restore_product(product_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Restore a hidden product."""
    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse("")
    product.is_hidden = False
    await session.commit()
    return HTMLResponse("")


@router.delete("/products/purge/{store}")
async def purge_products(store: str, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    store_enum = Store(store)
    product_ids = [r[0] for r in (await session.execute(
        select(Product.id).where(Product.store == store_enum)
    )).all()]
    if product_ids:
        await session.execute(delete(ProductMatch).where(
            (ProductMatch.product_a_id.in_(product_ids)) | (ProductMatch.product_b_id.in_(product_ids))
        ))
        await session.execute(delete(PriceHistory).where(PriceHistory.product_id.in_(product_ids)))
        await session.execute(delete(ConsumptionPrediction).where(ConsumptionPrediction.product_id.in_(product_ids)))
        await session.execute(delete(Product).where(Product.store == store_enum))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {len(product_ids)} {store} products.</span>'
    )
