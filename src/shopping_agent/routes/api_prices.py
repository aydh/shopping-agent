import asyncio
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Form
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import async_session, get_session
from ..models import PriceHistory, Product, ProductMatch, Store
from ..scrapers.coles import ColesScraper
from ..scrapers.woolworths import WoolworthsScraper

router = APIRouter()
logger = logging.getLogger(__name__)

# In-memory progress tracking: store_value -> {done, total, running}
_refresh_progress: dict[str, dict] = {}


@router.get("/image-proxy")
async def image_proxy(url: str):
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


async def _do_price_refresh(store_enum: Store) -> None:
    """Background task: refresh prices for all products of a given store."""
    scraper = ColesScraper() if store_enum == Store.COLES else WoolworthsScraper()
    concurrency = 10
    key = store_enum.value

    async with async_session() as session:
        result = await session.execute(select(Product).where(Product.store == store_enum))
        products = list(result.scalars().all())

    _refresh_progress[key] = {"done": 0, "total": len(products), "running": True}
    logger.info("[PriceRefresh] Starting %s refresh for %d products", store_enum.value, len(products))

    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(product_id: int, store_product_id: str, product_name: str):
        async with sem:
            try:
                scraped = await scraper.get_product_price(store_product_id, product_name)
                if scraped:
                    async with async_session() as session:
                        product = await session.get(Product, product_id)
                        if product:
                            product.current_price = scraped.current_price
                            product.is_available = scraped.is_available
                            if scraped.unit_price:
                                product.unit_price = scraped.unit_price
                            if scraped.unit_price_measure:
                                product.unit_price_measure = scraped.unit_price_measure
                            if scraped.image_url:
                                product.image_url = scraped.image_url
                            session.add(PriceHistory(product_id=product_id, store=store_enum, price=scraped.current_price))
                            await session.commit()
                    _refresh_progress[key]["done"] += 1
                    return True
            except Exception as e:
                logger.error("[PriceRefresh] Error for product %s: %s", store_product_id, e)
            _refresh_progress[key]["done"] += 1
            return False

    results = await asyncio.gather(*[fetch_one(p.id, p.store_product_id, p.name) for p in products])
    updated = sum(results)
    _refresh_progress[key] = {"done": len(products), "total": len(products), "running": False, "updated": updated}
    logger.info("[PriceRefresh] %s done: %d/%d updated", store_enum.value, updated, len(products))


@router.post("/refresh/{store}")
async def refresh_prices(store: str, background_tasks: BackgroundTasks, session: AsyncSession = Depends(get_session)):
    """Kick off a background price refresh for the given store."""
    store_enum = Store(store)
    scraper = ColesScraper() if store_enum == Store.COLES else WoolworthsScraper()

    if not await scraper.is_authenticated():
        return HTMLResponse(f'<span class="text-red-600 text-sm">Not connected to {store_enum.value.title()}.</span>')

    result = await session.execute(select(Product).where(Product.store == store_enum))
    count = len(result.scalars().all())
    if not count:
        return HTMLResponse(f'<span class="text-yellow-600 text-sm">No {store_enum.value.title()} products.</span>')

    background_tasks.add_task(_do_price_refresh, store_enum)
    store_val = store_enum.value
    return HTMLResponse(
        f'<span id="refresh-progress-{store_val}" class="text-blue-600 text-sm"'
        f' hx-get="/api/prices/refresh-progress/{store_val}"'
        f' hx-trigger="every 1s"'
        f' hx-target="#refresh-progress-{store_val}"'
        f' hx-swap="outerHTML">0/{count}</span>'
    )


@router.get("/refresh-progress/{store}")
async def refresh_progress(store: str):
    """Poll endpoint for price refresh progress."""
    state = _refresh_progress.get(store)
    if not state:
        return HTMLResponse("")
    done = state["done"]
    total = state["total"]
    running = state["running"]
    if running:
        return HTMLResponse(
            f'<span id="refresh-progress-{store}" class="text-blue-600 text-sm"'
            f' hx-get="/api/prices/refresh-progress/{store}"'
            f' hx-trigger="every 1s"'
            f' hx-target="#refresh-progress-{store}"'
            f' hx-swap="outerHTML">{done}/{total}</span>'
        )
    updated = state.get("updated", done)
    return HTMLResponse(
        f'<span class="text-green-600 text-sm">Done — {updated}/{total} updated</span>'
    )


@router.post("/confirm-match/{match_id}")
async def confirm_match(match_id: int, session: AsyncSession = Depends(get_session)):
    from sqlalchemy.orm import selectinload as sil
    from ..models import Store
    from ..services.price_comparison import PriceComparison
    from ..templating import templates

    match = await session.get(ProductMatch, match_id, options=[sil(ProductMatch.product_a), sil(ProductMatch.product_b)])
    if not match:
        return HTMLResponse("")

    match.is_confirmed = True
    await session.commit()

    pa, pb = match.product_a, match.product_b
    coles_p = pa if pa.store == Store.COLES else pb
    ww_p = pa if pa.store == Store.WOOLWORTHS else pb

    cp = coles_p.current_price
    wp = ww_p.current_price
    cheaper = None
    savings = 0.0
    if cp and wp:
        if cp < wp:
            cheaper = Store.COLES
            savings = wp - cp
        elif wp < cp:
            cheaper = Store.WOOLWORTHS
            savings = cp - wp

    comp = PriceComparison(
        product_name=coles_p.name,
        unit_size=coles_p.unit_size,
        product_id=coles_p.id,
        coles_product=coles_p,
        woolworths_product=ww_p,
        coles_price=cp,
        woolworths_price=wp,
        cheaper_store=cheaper,
        savings=savings,
        match_id=match.id,
        match_confidence=match.confidence,
        is_confirmed=match.is_confirmed,
        match_method=match.match_method,
    )

    html = templates.env.get_template("_match_row.html").render(comp=comp)
    return HTMLResponse(html)


@router.post("/manual-match")
async def create_manual_match(
    coles_id: int = Form(...),
    woolworths_id: int = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Create a manual match between a Coles and Woolworths product."""
    coles_product = await session.get(Product, coles_id)
    ww_product = await session.get(Product, woolworths_id)

    if not coles_product or not ww_product:
        return HTMLResponse('<p class="text-red-600 text-sm">Product not found.</p>', status_code=400)
    if coles_product.store != Store.COLES or ww_product.store != Store.WOOLWORTHS:
        return HTMLResponse('<p class="text-red-600 text-sm">Select one Coles and one Woolworths product.</p>', status_code=400)

    # Check if a match already exists for either product
    existing = await session.execute(
        select(ProductMatch).where(
            (ProductMatch.product_a_id == coles_id) | (ProductMatch.product_b_id == coles_id) |
            (ProductMatch.product_a_id == woolworths_id) | (ProductMatch.product_b_id == woolworths_id),
            ProductMatch.is_rejected == False,  # noqa: E712
        )
    )
    if existing.scalars().first():
        return HTMLResponse('<p class="text-red-600 text-sm">One of these products is already matched. Remove the existing match first.</p>', status_code=400)

    pm = ProductMatch(
        product_a_id=coles_id,
        product_b_id=woolworths_id,
        confidence=1.0,
        match_method="manual",
        is_confirmed=True,
    )
    session.add(pm)
    await session.commit()

    response = Response(status_code=200)
    response.headers["HX-Refresh"] = "true"
    return response


@router.get("/product-history/{product_id}")
async def product_price_history(product_id: int, session: AsyncSession = Depends(get_session)):
    """Return a chart + table of price history for a single product."""
    import json
    from ..models import PriceHistory

    product = await session.get(Product, product_id)
    if not product:
        return HTMLResponse("")

    from sqlalchemy import func as sqlfunc

    rows = (await session.execute(
        select(sqlfunc.date(PriceHistory.recorded_at), sqlfunc.avg(PriceHistory.price))
        .where(PriceHistory.product_id == product_id)
        .group_by(sqlfunc.date(PriceHistory.recorded_at))
        .order_by(sqlfunc.date(PriceHistory.recorded_at))
    )).all()

    from datetime import date as date_type
    def fmt(dt_str, f): return date_type.fromisoformat(dt_str).strftime(f)

    is_coles = product.store == Store.COLES
    color = "#dc2626" if is_coles else "#16a34a"
    label = "Coles" if is_coles else "Woolworths"

    points = [{"x": fmt(dt, "%d-%b"), "y": price} for dt, price in rows]

    if not points:
        return HTMLResponse('<div class="bg-gray-50 px-6 py-3 text-xs text-gray-400">No price history recorded yet.</div>')

    canvas_id = f"pchart-{product_id}"
    html = f"""
    <div class="bg-gray-50 px-3 sm:px-6 py-4 overflow-hidden">
      <div style="position:relative;max-width:100%">
      <canvas id="{canvas_id}" height="100"></canvas>
      </div>
      <script>
      (function() {{
        const ctx = document.getElementById('{canvas_id}').getContext('2d');
        new Chart(ctx, {{
          type: 'line',
          data: {{
            datasets: [{{
              label: '{label}',
              data: {json.dumps(points)},
              borderColor: '{color}',
              borderWidth: 1,
              pointBackgroundColor: '{color}',
              pointBorderColor: '{color}',
              pointRadius: 4,
              tension: 0.2,
              parsing: {{ xAxisKey: 'x', yAxisKey: 'y' }}
            }}]
          }},
          options: {{
            responsive: true,
            scales: {{
              x: {{ type: 'category', title: {{ display: false }} }},
              y: {{ title: {{ display: true, text: 'Price ($)' }}, beginAtZero: false }}
            }},
            plugins: {{ legend: {{ display: false }} }}
          }}
        }});
      }})();
      </script>
    </div>"""
    return HTMLResponse(html)


@router.get("/history/{match_id}")

async def price_history(match_id: int, session: AsyncSession = Depends(get_session)):
    """Return a chart + table of price history for a matched product pair."""
    import json
    from ..models import PriceHistory
    from sqlalchemy.orm import selectinload as sil

    match = await session.get(ProductMatch, match_id, options=[sil(ProductMatch.product_a), sil(ProductMatch.product_b)])
    if not match:
        return HTMLResponse("")

    pa, pb = match.product_a, match.product_b
    coles_p = pa if pa.store == Store.COLES else pb
    ww_p = pa if pa.store == Store.WOOLWORTHS else pb

    from sqlalchemy import func as sqlfunc

    coles_rows = (await session.execute(
        select(sqlfunc.date(PriceHistory.recorded_at), sqlfunc.avg(PriceHistory.price))
        .where(PriceHistory.product_id == coles_p.id)
        .group_by(sqlfunc.date(PriceHistory.recorded_at))
        .order_by(sqlfunc.date(PriceHistory.recorded_at))
    )).all()

    ww_rows = (await session.execute(
        select(sqlfunc.date(PriceHistory.recorded_at), sqlfunc.avg(PriceHistory.price))
        .where(PriceHistory.product_id == ww_p.id)
        .group_by(sqlfunc.date(PriceHistory.recorded_at))
        .order_by(sqlfunc.date(PriceHistory.recorded_at))
    )).all()

    from datetime import date as date_type
    def fmt(dt_str, fmt): return date_type.fromisoformat(dt_str).strftime(fmt)

    coles_points = [{"x": fmt(dt, "%d-%b"), "y": price} for dt, price in coles_rows]
    ww_points = [{"x": fmt(dt, "%d-%b"), "y": price} for dt, price in ww_rows]

    if not coles_points and not ww_points:
        return HTMLResponse('<div class="bg-gray-50 px-6 py-3 text-xs text-gray-400">No price history recorded yet.</div>')

    # Merge and sort by ISO date string (chronological) before formatting for display
    all_combined = [{"x": fmt(dt, "%d-%b"), "y": price}
                    for dt, price in sorted(list(coles_rows) + list(ww_rows), key=lambda r: r[0])]

    canvas_id = f"chart-{match_id}"
    html = f"""
    <div class="bg-gray-50 px-3 sm:px-6 py-4 overflow-hidden">
      <div style="position:relative;max-width:100%">
      <canvas id="{canvas_id}" height="100"></canvas>
      </div>
      <script>
        (function() {{
          const ctx = document.getElementById('{canvas_id}').getContext('2d');
          const allPoints = {json.dumps(all_combined)};
          new Chart(ctx, {{
            type: 'line',
            data: {{
              datasets: [
                {{
                  label: 'Price',
                  data: allPoints,
                  borderColor: '#111827',
                  borderWidth: 1,
                  pointRadius: 0,
                  tension: 0.2,
                  parsing: {{ xAxisKey: 'x', yAxisKey: 'y' }}
                }},
                {{
                  label: 'Coles',
                  data: {json.dumps(coles_points)},
                  borderColor: 'transparent',
                  pointBackgroundColor: '#dc2626',
                  pointBorderColor: '#dc2626',
                  pointRadius: 6,
                  showLine: false,
                  parsing: {{ xAxisKey: 'x', yAxisKey: 'y' }}
                }},
                {{
                  label: 'Woolworths',
                  data: {json.dumps(ww_points)},
                  borderColor: 'transparent',
                  pointBackgroundColor: '#16a34a',
                  pointBorderColor: '#16a34a',
                  pointRadius: 6,
                  showLine: false,
                  parsing: {{ xAxisKey: 'x', yAxisKey: 'y' }}
                }}
              ]
            }},
            options: {{
              responsive: true,
              scales: {{
                x: {{ type: 'category', title: {{ display: false }} }},
                y: {{ title: {{ display: true, text: 'Price ($)' }}, beginAtZero: false }}
              }},
              plugins: {{ legend: {{ position: 'top' }} }}
            }}
          }});
        }})();
        </script>
    </div>"""
    return HTMLResponse(html)


@router.delete("/matches/purge")
async def purge_all_matches(session: AsyncSession = Depends(get_session)):
    result = await session.execute(delete(ProductMatch))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} product matches.</span>'
    )


@router.delete("/products/purge/{store}")
async def purge_products(store: str, session: AsyncSession = Depends(get_session)):
    from ..models import PriceHistory, ConsumptionPrediction
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


@router.delete("/history/purge")
async def purge_price_history(session: AsyncSession = Depends(get_session)):
    from ..models import PriceHistory
    result = await session.execute(delete(PriceHistory))
    await session.commit()
    return HTMLResponse(
        f'<span class="text-orange-600 text-sm">Purged {result.rowcount} price history records.</span>'
    )


@router.delete("/match/{match_id}")
async def delete_match(match_id: int, session: AsyncSession = Depends(get_session)):
    """Reject a product match so it is never auto-matched again."""
    match = await session.get(ProductMatch, match_id)
    if not match:
        return HTMLResponse("", status_code=404)
    match.is_rejected = True
    match.is_confirmed = False
    await session.commit()
    return HTMLResponse("")
