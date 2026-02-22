import logging
import re
from datetime import datetime

from .base import BaseScraper, ScrapedOrder, ScrapedOrderItem, ScrapedProduct
from .browser_manager import browser_manager
from ..models.product import Store

logger = logging.getLogger(__name__)

WOOLWORTHS_BASE = "https://www.woolworths.com.au"


class WoolworthsScraper(BaseScraper):
    store = Store.WOOLWORTHS

    async def is_authenticated(self) -> bool:
        return await browser_manager.is_authenticated(Store.WOOLWORTHS)

    async def login_interactive(self) -> bool:
        return await browser_manager.login_interactive(Store.WOOLWORTHS)

    async def get_order_history(self, limit: int = 50) -> list[ScrapedOrder]:
        page = await browser_manager.get_page(Store.WOOLWORTHS)
        orders: list[ScrapedOrder] = []

        try:
            # First try the internal API for past shops
            orders = await self._fetch_via_api(page, limit)

            if not orders:
                # Fallback to page scraping
                await page.goto(
                    f"{WOOLWORTHS_BASE}/shop/myaccount/myorders", wait_until="networkidle"
                )
                await page.wait_for_timeout(3000)
                orders = await self._scrape_orders_page(page, limit)

            await browser_manager.save_all_cookies()
        except Exception:
            logger.exception("Failed to fetch Woolworths order history")
        finally:
            await page.close()

        return orders

    async def _fetch_via_api(self, page, limit: int) -> list[ScrapedOrder]:
        """Fetch past orders via Woolworths internal API."""
        orders = []
        try:
            # Navigate to woolworths first to get proper context
            await page.goto(WOOLWORTHS_BASE, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            result = await page.evaluate(
                """
                async (limit) => {
                    try {
                        // Try the past shops API
                        let resp = await fetch('/apis/ui/PastOrder/Orders?pageNumber=1&pageSize=' + limit, {
                            credentials: 'include',
                            headers: { 'Accept': 'application/json' }
                        });
                        if (resp.ok) return await resp.json();

                        // Try alternate endpoint
                        resp = await fetch('/api/v3/ui/orders?page=1&size=' + limit, {
                            credentials: 'include',
                            headers: { 'Accept': 'application/json' }
                        });
                        if (resp.ok) return await resp.json();
                        return null;
                    } catch(e) { return null; }
                }
            """,
                limit,
            )

            if result:
                order_list = (
                    result.get("Orders")
                    or result.get("orders")
                    or result.get("data")
                    or []
                )
                for order_data in order_list[:limit]:
                    order = self._parse_api_order(order_data)
                    if order:
                        orders.append(order)
        except Exception:
            logger.debug("Woolworths API order fetch failed")

        return orders

    async def _scrape_orders_page(self, page, limit: int) -> list[ScrapedOrder]:
        """Scrape orders from the rendered DOM."""
        orders = []
        try:
            # Try to find order data in page scripts
            script_data = await page.evaluate(
                """
                () => {
                    const results = [];
                    // Check for __NEXT_DATA__ or similar
                    if (window.__NEXT_DATA__) results.push(window.__NEXT_DATA__);
                    if (window.__WOW_INITIAL_STATE__) results.push(window.__WOW_INITIAL_STATE__);
                    const scripts = document.querySelectorAll('script[type="application/json"]');
                    scripts.forEach(s => {
                        try { results.push(JSON.parse(s.textContent)); } catch(e) {}
                    });
                    return results;
                }
            """
            )
            for data in script_data or []:
                orders.extend(self._extract_orders_from_json(data))
        except Exception:
            logger.debug("Woolworths DOM scraping failed")

        return orders[:limit]

    def _extract_orders_from_json(self, data) -> list[ScrapedOrder]:
        """Recursively search for order data in JSON."""
        orders = []
        if isinstance(data, dict):
            if "OrderId" in data or "orderId" in data or "orderNumber" in data:
                order = self._parse_api_order(data)
                if order:
                    orders.append(order)
            for value in data.values():
                if isinstance(value, (dict, list)):
                    orders.extend(self._extract_orders_from_json(value))
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    orders.extend(self._extract_orders_from_json(item))
        return orders

    def _parse_api_order(self, data: dict) -> ScrapedOrder | None:
        try:
            order_id = str(
                data.get("OrderId") or data.get("orderId") or data.get("orderNumber") or data.get("id", "")
            )
            if not order_id:
                return None

            date_str = (
                data.get("OrderDate")
                or data.get("orderDate")
                or data.get("DeliveryDate")
                or data.get("deliveryDate")
                or ""
            )
            try:
                order_date = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                order_date = datetime.now().date()

            items = []
            for item_data in (
                data.get("OrderItems") or data.get("items") or data.get("Products") or []
            ):
                item = ScrapedOrderItem(
                    store_product_id=str(
                        item_data.get("Stockcode")
                        or item_data.get("stockcode")
                        or item_data.get("productId")
                        or item_data.get("id", "")
                    ),
                    name=(
                        item_data.get("DisplayName")
                        or item_data.get("name")
                        or item_data.get("Name")
                        or "Unknown"
                    ),
                    quantity=int(item_data.get("Quantity") or item_data.get("quantity") or 1),
                    price_paid=float(
                        item_data.get("SalePrice")
                        or item_data.get("price")
                        or item_data.get("Price")
                        or 0
                    ),
                    brand=item_data.get("Brand") or item_data.get("brand"),
                    unit_size=(
                        item_data.get("PackageSize")
                        or item_data.get("packageSize")
                        or item_data.get("Size")
                    ),
                    image_url=(
                        item_data.get("MediumImageFile")
                        or item_data.get("imageUrl")
                        or item_data.get("ImageUrl")
                    ),
                )
                items.append(item)

            return ScrapedOrder(
                store_order_id=order_id,
                order_date=order_date,
                total_amount=float(
                    data.get("TotalPrice") or data.get("totalAmount") or data.get("Total") or 0
                ),
                status=data.get("Status") or data.get("status"),
                items=items,
            )
        except Exception:
            logger.debug("Failed to parse Woolworths order", exc_info=True)
            return None

    async def search_product(self, query: str) -> list[ScrapedProduct]:
        page = await browser_manager.get_page(Store.WOOLWORTHS)
        products = []
        try:
            await page.goto(WOOLWORTHS_BASE, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

            result = await page.evaluate(
                """
                async (query) => {
                    try {
                        const resp = await fetch(
                            `/apis/ui/Search/products?searchTerm=${encodeURIComponent(query)}&pageNumber=1&pageSize=20&sortType=TraderRelevance`,
                            { credentials: 'include', headers: { 'Accept': 'application/json' } }
                        );
                        if (resp.ok) return await resp.json();
                        return null;
                    } catch(e) { return null; }
                }
            """,
                query,
            )

            if result:
                for item in (
                    result.get("Products") or result.get("products") or result.get("Items") or []
                ):
                    # Woolworths wraps products in a Products array
                    product_data = item.get("Products", [item]) if isinstance(item, dict) else [item]
                    for pd in product_data:
                        p = self._parse_search_result(pd)
                        if p:
                            products.append(p)
        except Exception:
            logger.exception("Woolworths search failed for: %s", query)
        finally:
            await page.close()

        return products

    def _parse_search_result(self, data: dict) -> ScrapedProduct | None:
        try:
            return ScrapedProduct(
                store_product_id=str(data.get("Stockcode") or data.get("stockcode") or ""),
                name=data.get("Name") or data.get("name") or data.get("DisplayName") or "",
                current_price=float(data.get("Price") or data.get("price") or 0),
                brand=data.get("Brand") or data.get("brand"),
                category=data.get("Category") or data.get("category"),
                unit_size=data.get("PackageSize") or data.get("packageSize"),
                unit_price=float(data.get("CupPrice") or data.get("unitPrice") or 0) or None,
                unit_price_measure=data.get("CupMeasure") or data.get("cupMeasure"),
                image_url=data.get("MediumImageFile") or data.get("imageUrl"),
                product_url=data.get("UrlFriendlyName"),
                is_available=data.get("IsAvailable", True),
            )
        except Exception:
            return None

    async def get_product_price(self, store_product_id: str) -> ScrapedProduct | None:
        page = await browser_manager.get_page(Store.WOOLWORTHS)
        try:
            await page.goto(WOOLWORTHS_BASE, wait_until="domcontentloaded")
            result = await page.evaluate(
                """
                async (productId) => {
                    try {
                        const resp = await fetch(
                            `/apis/ui/product/detail/${productId}`,
                            { credentials: 'include', headers: { 'Accept': 'application/json' } }
                        );
                        if (resp.ok) return await resp.json();
                        return null;
                    } catch(e) { return null; }
                }
            """,
                store_product_id,
            )
            if result:
                product_data = result.get("Product") or result
                return self._parse_search_result(product_data)
        except Exception:
            logger.exception("Woolworths price fetch failed for: %s", store_product_id)
        finally:
            await page.close()
        return None

    async def add_to_cart(self, items: list[tuple[str, int]]) -> bool:
        page = await browser_manager.get_page(Store.WOOLWORTHS)
        try:
            await page.goto(WOOLWORTHS_BASE, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

            for product_id, quantity in items:
                result = await page.evaluate(
                    """
                    async ([productId, qty]) => {
                        try {
                            const resp = await fetch('/apis/ui/Trolley/item', {
                                method: 'POST',
                                credentials: 'include',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'Accept': 'application/json'
                                },
                                body: JSON.stringify({
                                    Stockcode: parseInt(productId),
                                    Quantity: qty,
                                    IsInTrolley: false
                                })
                            });
                            return resp.ok;
                        } catch(e) { return false; }
                    }
                """,
                    [product_id, quantity],
                )
                if not result:
                    logger.warning("Failed to add Woolworths product %s to cart", product_id)
                    return False

            await browser_manager.save_all_cookies()
            return True
        except Exception:
            logger.exception("Woolworths add to cart failed")
            return False
        finally:
            await page.close()

    async def get_cart_url(self) -> str:
        return f"{WOOLWORTHS_BASE}/shop/checkout"


woolworths_scraper = WoolworthsScraper()
