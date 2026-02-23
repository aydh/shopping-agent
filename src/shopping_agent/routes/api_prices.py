import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import Product, ProductMatch, Store
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
