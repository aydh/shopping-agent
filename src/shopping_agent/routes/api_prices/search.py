"""Search-based product matching."""
import logging

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...models import Product, ProductMatch, Store
from ...scrapers.coles import coles_scraper
from ...scrapers.woolworths import woolworths_scraper
from ...templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/search-match/{product_id}")
async def search_match(
    product_id: int,
    request: Request,
    q: str = Query(default=""),
    session: AsyncSession = Depends(get_session),
):
    """Search the opposite store for products to match against the given product."""
    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse("")

    target_store = Store.WOOLWORTHS if product.store == Store.COLES else Store.COLES

    if not q.strip():
        return HTMLResponse("")

    scraper = woolworths_scraper if target_store == Store.WOOLWORTHS else coles_scraper

    try:
        results = await scraper.search_product(q.strip())
    except Exception as e:
        logger.error("Search failed: %s", e)
        return HTMLResponse('<p class="text-sm text-red-600">Search failed. Are you authenticated?</p>')

    return templates.TemplateResponse(
        "_search_match_results.html",
        {
            "request": request,
            "results": results,
            "source_product_id": product_id,
            "target_store": target_store.value,
        },
    )


@router.post("/search-match/confirm")
async def confirm_search_match(
    source_product_id: int = Form(...),
    store_product_id: str = Form(...),
    store: str = Form(...),
    name: str = Form(...),
    brand: str = Form(default=""),
    unit_size: str = Form(default=""),
    current_price: float = Form(...),
    unit_price: float = Form(default=0.0),
    unit_price_measure: str = Form(default=""),
    image_url: str = Form(default=""),
    product_url: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
):
    """Upsert a searched product and create a manual match."""
    target_store = Store(store)

    # Upsert the searched product
    result = await session.execute(
        select(Product).where(
            Product.store == target_store,
            Product.store_product_id == store_product_id,
        )
    )
    target_product = result.scalar_one_or_none()
    if target_product:
        target_product.current_price = current_price
        target_product.is_available = True
    else:
        target_product = Product(
            store=target_store,
            store_product_id=store_product_id,
            name=name,
            brand=brand or None,
            unit_size=unit_size or None,
            current_price=current_price,
            unit_price=unit_price or None,
            unit_price_measure=unit_price_measure or None,
            image_url=image_url or None,
            product_url=product_url or None,
        )
        session.add(target_product)
        await session.flush()

    # Determine coles_id / woolworths_id
    if target_store == Store.WOOLWORTHS:
        coles_id, woolworths_id = source_product_id, target_product.id
    else:
        coles_id, woolworths_id = target_product.id, source_product_id

    # Check no active match already exists for either product
    existing = await session.execute(
        select(ProductMatch).where(
            (ProductMatch.product_a_id == coles_id) | (ProductMatch.product_b_id == coles_id) |
            (ProductMatch.product_a_id == woolworths_id) | (ProductMatch.product_b_id == woolworths_id),
            ProductMatch.is_rejected == False,  # noqa: E712
        )
    )
    if existing.scalars().first():
        await session.rollback()
        return HTMLResponse(
            '<p class="text-red-600 text-sm">One of these products is already matched.</p>',
            status_code=400,
        )

    pm = ProductMatch(
        product_a_id=coles_id,
        product_b_id=woolworths_id,
        confidence=1.0,
        match_method="manual",
        is_confirmed=True,
    )
    session.add(pm)
    await session.commit()

    return RedirectResponse("/prices", status_code=303)
