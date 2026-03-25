"""Product lookup API — search both stores and select a product to match."""
import asyncio
import logging

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...db_helpers import store_from_string
from ...models import Product, Store
from ...scrapers.coles import coles_scraper
from ...scrapers.woolworths import woolworths_scraper
from ...templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_LOOKUP_RESULTS_PER_STORE = 10


async def _safe_search(scraper, q: str) -> list:
    try:
        return await scraper.search_product(q)
    except Exception as e:
        logger.error("Product lookup search failed for %s: %s", scraper.__class__.__name__, e)
        return []


@router.get("/product-lookup/search")
async def product_lookup_search(
    request: Request,
    q: str = Query(default=""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search both stores in parallel and return combined results."""
    if not q.strip():
        return HTMLResponse("")

    coles_results, woolworths_results = await asyncio.gather(
        _safe_search(coles_scraper, q.strip()),
        _safe_search(woolworths_scraper, q.strip()),
    )

    # Filter out products already in the database, then limit
    all_ids = {r.store_product_id for r in coles_results} | {r.store_product_id for r in woolworths_results}
    if all_ids:
        existing = await session.execute(
            select(Product.store, Product.store_product_id).where(
                Product.store_product_id.in_(all_ids)
            )
        )
        existing_ids: set[tuple] = {(row.store.value, row.store_product_id) for row in existing}
    else:
        existing_ids = set()

    coles_results = [
        r for r in coles_results if ("coles", r.store_product_id) not in existing_ids
    ][:MAX_LOOKUP_RESULTS_PER_STORE]
    woolworths_results = [
        r for r in woolworths_results if ("woolworths", r.store_product_id) not in existing_ids
    ][:MAX_LOOKUP_RESULTS_PER_STORE]

    return templates.TemplateResponse(
        request,
        "_product_lookup_results.html",
        {
            "coles_results": coles_results,
            "woolworths_results": woolworths_results,
        },
    )


@router.post("/product-lookup/select")
async def product_lookup_select(
    request: Request,
    store: str = Form(...),
    store_product_id: str = Form(...),
    name: str = Form(...),
    brand: str = Form(default=""),
    unit_size: str = Form(default=""),
    current_price: float = Form(...),
    unit_price: float = Form(default=0.0),
    unit_price_measure: str = Form(default=""),
    image_url: str = Form(default=""),
    product_url: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Upsert the selected product and return the match-search panel."""
    source_store = store_from_string(store)

    result = await session.execute(
        select(Product).where(
            Product.store == source_store,
            Product.store_product_id == store_product_id,
        )
    )
    product = result.scalar_one_or_none()

    if product:
        product.current_price = current_price
        product.is_available = True
    else:
        product = Product(
            store=source_store,
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
        session.add(product)

    await session.commit()
    await session.refresh(product)

    target_store = Store.WOOLWORTHS if source_store == Store.COLES else Store.COLES

    return templates.TemplateResponse(
        request,
        "_product_lookup_selected.html",
        {
            "product": product,
            "target_store": target_store.value,
        },
    )
