import json
import logging
import re
from datetime import datetime

from .base import BaseScraper, ScrapedOrder, ScrapedOrderItem, ScrapedProduct
from .browser_manager import browser_manager
from ..models.product import Store

logger = logging.getLogger(__name__)

COLES_BASE = "https://www.coles.com.au"
COLES_API = "https://www.coles.com.au/api"


class ColesScraper(BaseScraper):
    store = Store.COLES

    async def is_authenticated(self) -> bool:
        return await browser_manager.is_authenticated(Store.COLES)

    async def login_interactive(self) -> bool:
        return await browser_manager.login_interactive(Store.COLES)

    async def get_order_history(self, limit: int = 50) -> list[ScrapedOrder]:
        page = await browser_manager.get_page(Store.COLES)
        orders: list[ScrapedOrder] = []

        try:
            await page.goto(f"{COLES_BASE}/customer/orders", wait_until="networkidle")
            await page.wait_for_timeout(3000)

            # Try to extract orders from the page DOM
            # Coles renders order history client-side; structure may vary
            order_elements = await page.query_selector_all('[data-testid*="order"], .order-card, .order-item')

            if not order_elements:
                # Fallback: try to extract from page content via API intercept
                orders = await self._fetch_orders_via_api(page, limit)
            else:
                for elem in order_elements[:limit]:
                    order = await self._parse_order_element(page, elem)
                    if order:
                        orders.append(order)

            await browser_manager.save_all_cookies()
        except Exception:
            logger.exception("Failed to fetch Coles order history")
        finally:
            await page.close()

        return orders

    async def _fetch_orders_via_api(self, page, limit: int) -> list[ScrapedOrder]:
        """Try to fetch orders via Coles internal API from within browser context."""
        orders = []
        try:
            result = await page.evaluate("""
                async () => {
                    try {
                        const resp = await fetch('/api/orders', {
                            credentials: 'include',
                            headers: { 'Accept': 'application/json' }
                        });
                        if (resp.ok) return await resp.json();
                        return null;
                    } catch(e) { return null; }
                }
            """)
            if result and isinstance(result, dict):
                for order_data in (result.get("orders") or result.get("data") or [])[:limit]:
                    order = self._parse_api_order(order_data)
                    if order:
                        orders.append(order)
        except Exception:
            logger.debug("Coles API order fetch failed, trying DOM scraping")

        if not orders:
            orders = await self._scrape_orders_from_dom(page, limit)

        return orders

    async def _scrape_orders_from_dom(self, page, limit: int) -> list[ScrapedOrder]:
        """Last resort: scrape order info from whatever DOM is rendered."""
        orders = []
        try:
            content = await page.content()
            # Look for JSON data embedded in the page
            script_data = await page.evaluate("""
                () => {
                    const scripts = document.querySelectorAll('script[type="application/json"]');
                    const results = [];
                    scripts.forEach(s => {
                        try { results.push(JSON.parse(s.textContent)); } catch(e) {}
                    });
                    // Also check __NEXT_DATA__
                    if (window.__NEXT_DATA__) results.push(window.__NEXT_DATA__);
                    return results;
                }
            """)
            for data in (script_data or []):
                embedded_orders = self._extract_orders_from_json(data)
                orders.extend(embedded_orders)
        except Exception:
            logger.debug("DOM scraping failed for Coles orders")
        return orders[:limit]

    def _extract_orders_from_json(self, data: dict | list) -> list[ScrapedOrder]:
        """Recursively search JSON structure for order data."""
        orders = []
        if isinstance(data, dict):
            # Look for order-like structures
            if "orderId" in data or "orderNumber" in data:
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
        """Parse an order from Coles API JSON."""
        try:
            order_id = str(data.get("orderId") or data.get("orderNumber") or data.get("id", ""))
            if not order_id:
                return None

            date_str = data.get("orderDate") or data.get("deliveryDate") or data.get("date", "")
            try:
                order_date = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                order_date = datetime.now().date()

            items = []
            for item_data in data.get("items") or data.get("lineItems") or []:
                item = ScrapedOrderItem(
                    store_product_id=str(
                        item_data.get("productId") or item_data.get("sku") or item_data.get("id", "")
                    ),
                    name=item_data.get("name") or item_data.get("productName") or "Unknown",
                    quantity=int(item_data.get("quantity", 1)),
                    price_paid=float(item_data.get("price") or item_data.get("totalPrice") or 0),
                    brand=item_data.get("brand"),
                    unit_size=item_data.get("size") or item_data.get("packageSize"),
                    image_url=item_data.get("imageUrl") or item_data.get("image"),
                )
                items.append(item)

            return ScrapedOrder(
                store_order_id=order_id,
                order_date=order_date,
                total_amount=float(data.get("totalAmount") or data.get("total") or 0),
                status=data.get("status"),
                items=items,
            )
        except Exception:
            logger.debug("Failed to parse Coles order", exc_info=True)
            return None

    async def _parse_order_element(self, page, elem) -> ScrapedOrder | None:
        """Parse an order from a DOM element."""
        try:
            text = await elem.inner_text()
            # Extract order ID and date from text content
            order_id_match = re.search(r"#?(\d{8,})", text)
            date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)

            if not order_id_match:
                return None

            order_date = datetime.now().date()
            if date_match:
                try:
                    order_date = datetime.strptime(date_match.group(1), "%d/%m/%Y").date()
                except ValueError:
                    pass

            return ScrapedOrder(
                store_order_id=order_id_match.group(1),
                order_date=order_date,
                items=[],
            )
        except Exception:
            return None

    async def search_product(self, query: str) -> list[ScrapedProduct]:
        page = await browser_manager.get_page(Store.COLES)
        products = []
        try:
            # Use Coles search API via browser context
            result = await page.evaluate(
                """
                async (query) => {
                    try {
                        const resp = await fetch(
                            `/api/search/v1?query=${encodeURIComponent(query)}&page=1&pageSize=20`,
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
                for item in result.get("results") or result.get("products") or []:
                    products.append(self._parse_search_result(item))
        except Exception:
            logger.exception("Coles search failed for: %s", query)
        finally:
            await page.close()

        return [p for p in products if p is not None]

    def _parse_search_result(self, data: dict) -> ScrapedProduct | None:
        try:
            return ScrapedProduct(
                store_product_id=str(data.get("id") or data.get("sku") or ""),
                name=data.get("name") or data.get("title") or "",
                current_price=float(data.get("price") or data.get("salePrice") or 0),
                brand=data.get("brand"),
                category=data.get("category"),
                unit_size=data.get("size") or data.get("packageSize"),
                unit_price=float(data.get("unitPrice") or 0) or None,
                unit_price_measure=data.get("unitPriceMeasure"),
                image_url=data.get("imageUrl") or data.get("image"),
                product_url=data.get("url"),
                is_available=data.get("availability", True),
            )
        except Exception:
            return None

    async def get_product_price(self, store_product_id: str) -> ScrapedProduct | None:
        page = await browser_manager.get_page(Store.COLES)
        try:
            result = await page.evaluate(
                """
                async (productId) => {
                    try {
                        const resp = await fetch(
                            `/api/products/${productId}`,
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
                return self._parse_search_result(result)
        except Exception:
            logger.exception("Coles price fetch failed for: %s", store_product_id)
        finally:
            await page.close()
        return None

    async def add_to_cart(self, items: list[tuple[str, int]]) -> bool:
        page = await browser_manager.get_page(Store.COLES)
        try:
            for product_id, quantity in items:
                result = await page.evaluate(
                    """
                    async ([productId, qty]) => {
                        try {
                            const resp = await fetch('/api/cart/items', {
                                method: 'POST',
                                credentials: 'include',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'Accept': 'application/json'
                                },
                                body: JSON.stringify({ productId, quantity: qty })
                            });
                            return resp.ok;
                        } catch(e) { return false; }
                    }
                """,
                    [product_id, quantity],
                )
                if not result:
                    logger.warning("Failed to add Coles product %s to cart", product_id)
                    return False
            await browser_manager.save_all_cookies()
            return True
        except Exception:
            logger.exception("Coles add to cart failed")
            return False
        finally:
            await page.close()

    async def get_cart_url(self) -> str:
        return f"{COLES_BASE}/cart"


coles_scraper = ColesScraper()
