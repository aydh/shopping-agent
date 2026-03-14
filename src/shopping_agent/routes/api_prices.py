import logging

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import PriceHistory, Product, ProductMatch, Store
from ..scrapers.coles import ColesScraper
from ..scrapers.woolworths import WoolworthsScraper

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/refresh")
async def refresh_prices(session: AsyncSession = Depends(get_session)):
    """Refresh prices for all products by scraping current prices from stores."""
    coles_scraper = ColesScraper()
    woolworths_scraper = WoolworthsScraper()

    try:
        # Check authentication
        coles_auth = await coles_scraper.is_authenticated()
        woolworths_auth = await woolworths_scraper.is_authenticated()

        logger.info(f"Authentication status - Coles: {coles_auth}, Woolworths: {woolworths_auth}")

        if not coles_auth and not woolworths_auth:
            return HTMLResponse(
                '<tr><td colspan="6" class="px-6 py-4 text-center text-red-600 text-sm">'
                'Not authenticated with any stores. Please log in first.</td></tr>'
            )

        # Get all products
        result = await session.execute(select(Product))
        products = result.scalars().all()

        if not products:
            return HTMLResponse(
                '<tr><td colspan="6" class="px-6 py-4 text-center text-yellow-600 text-sm">'
                'No products to refresh.</td></tr>'
            )

        updated_count = 0
        failed_count = 0
        auth_error_count = 0

        for product in products:
            try:
                # Select appropriate scraper and check auth
                if product.store == Store.COLES:
                    scraper = coles_scraper
                    if not coles_auth:
                        auth_error_count += 1
                        logger.warning(f"Skipping Coles product {product.id}: not authenticated")
                        continue
                else:
                    scraper = woolworths_scraper
                    if not woolworths_auth:
                        auth_error_count += 1
                        logger.warning(f"Skipping Woolworths product {product.id}: not authenticated")
                        continue

                # Fetch current price
                scraped = await scraper.get_product_price(product.store_product_id)

                if scraped:
                    product.current_price = scraped.current_price
                    product.is_available = scraped.is_available
                    if scraped.unit_price:
                        product.unit_price = scraped.unit_price
                    if scraped.unit_price_measure:
                        product.unit_price_measure = scraped.unit_price_measure
                    session.add(PriceHistory(product_id=product.id, store=product.store, price=scraped.current_price))
                    updated_count += 1
                    logger.info(f"Updated price for product {product.id}: ${scraped.current_price}")
                else:
                    failed_count += 1
                    logger.warning(f"Could not fetch price for product {product.id}")

            except Exception as e:
                failed_count += 1
                logger.error(f"Error fetching price for product {product.id}: {e}")

        # Commit all updates
        await session.commit()

        if updated_count == 0 and failed_count > 0:
            message = f"Failed to fetch any prices. {failed_count} products skipped"
            if auth_error_count > 0:
                message += f" ({auth_error_count} due to auth issues)"
            return HTMLResponse(
                f'<tr><td colspan="6" class="px-6 py-4 text-center text-red-600 text-sm">'
                f'{message}</td></tr>'
            )

        # If we updated any prices, refresh the page to show updated data
        if updated_count > 0:
            response = Response(status_code=200)
            response.headers["HX-Refresh"] = "true"
            return response

        message = f"Updated {updated_count} price"
        message += "s" if updated_count != 1 else ""
        if failed_count > 0:
            message += f", {failed_count} failed"
        message += ". <a href=\"/prices\" class=\"underline\">Reload</a> to see updates."

        return HTMLResponse(
            f'<tr><td colspan="6" class="px-6 py-4 text-center text-green-600 text-sm">'
            f'{message}</td></tr>'
        )

    except Exception as e:
        logger.error(f"Error during price refresh: {e}")
        return HTMLResponse(
            '<tr><td colspan="6" class="px-6 py-4 text-center text-red-600 text-sm">'
            f'Error refreshing prices: {str(e)}</td></tr>'
        )


@router.post("/confirm-match/{match_id}")
async def confirm_match(match_id: int, session: AsyncSession = Depends(get_session)):
    match = await session.get(ProductMatch, match_id)
    if match:
        match.is_confirmed = True
        await session.commit()
        return HTMLResponse(
            '<td colspan="6" class="px-6 py-4 text-center text-green-600 text-sm">'
            'Match confirmed. <a href="/prices" class="underline">Reload</a> to see updates.</td>'
        )
    return HTMLResponse("")


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
            (ProductMatch.product_a_id == woolworths_id) | (ProductMatch.product_b_id == woolworths_id)
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
    <div class="bg-gray-50 px-6 py-4">
      <canvas id="{canvas_id}" height="100"></canvas>
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
    <div class="bg-gray-50 px-6 py-4">
      <canvas id="{canvas_id}" height="100"></canvas>
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
    """Remove a product match."""
    match = await session.get(ProductMatch, match_id)
    if match:
        await session.delete(match)
        await session.commit()
        response = Response(status_code=200)
        response.headers["HX-Refresh"] = "true"
        return response
    return HTMLResponse("", status_code=404)
