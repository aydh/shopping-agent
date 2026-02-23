import json
import logging
import time
from datetime import datetime

import httpx
from sqlalchemy import select

from ..database import async_session
from ..models.product import Store
from ..models.store_cookies import StoreCookies
from .base import BaseScraper, ScrapedOrder, ScrapedOrderItem, ScrapedProduct

logger = logging.getLogger(__name__)

COLES_BASE = "https://www.coles.com.au"
COLES_GRAPHQL_URL = f"{COLES_BASE}/api/graphql"
COLES_STORE_ID = "COL:7674"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Origin": COLES_BASE,
    "Referer": f"{COLES_BASE}/",
    "Ocp-Apim-Subscription-Key": "eae83861d1cd4de6bb9cd8a2cd6f041e",
    "dsch-channel": "coles.online.1site.desktop",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# ── GraphQL queries ───────────────────────────────────────────────────────────

_GQL_PRODUCT_FIELDS = """
    id
    name
    brand
    description
    imageUris { uri }
    size
    pricing {
        now
        was
        unit { price }
        promotionType
        saveAmount
    }
"""

_GQL_SEARCH = """
query SearchProducts(
    $searchTerm: String!,
    $storeId: BrandedId!,
    $pageNumber: Int = 1,
    $pageSize: Int = 48
) {
    searchProducts(input: {
        searchTerm: $searchTerm
        storeId: $storeId
        pagination: { pageNumber: $pageNumber pageSize: $pageSize }
    }) {
        results {
            """ + _GQL_PRODUCT_FIELDS + """
        }
    }
}
"""

_GQL_CROSS_CATEGORY = """
query GetCrossCategory(
    $categoryIds: [ID!]!,
    $storeId: BrandedId!,
    $memoryToken: String
) {
    crossCategory(
        categoryIds: $categoryIds
        storeId: $storeId
        memoryToken: $memoryToken
    ) {
        products {
            """ + _GQL_PRODUCT_FIELDS + """
        }
        memoryToken
    }
}
"""


class ColesScraper(BaseScraper):
    store = Store.COLES

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _load_cookies(self) -> httpx.Cookies:
        """Load cookies from the database into httpx.Cookies."""
        jar = httpx.Cookies()
        async with async_session() as session:
            result = await session.execute(
                select(StoreCookies).where(StoreCookies.store == Store.COLES)
            )
            row = result.scalar_one_or_none()
            if row:
                try:
                    raw_cookies = json.loads(row.cookies_json)
                    for c in raw_cookies:
                        jar.set(
                            c["name"],
                            c["value"],
                            domain=c.get("domain", ".coles.com.au"),
                            path=c.get("path", "/"),
                        )
                    logger.info(
                        "Loaded %d cookies for coles: %s",
                        len(raw_cookies),
                        [f"{c['name']}@{c.get('domain','?')}" for c in raw_cookies],
                    )
                except Exception:
                    logger.warning("Failed to load Coles cookies", exc_info=True)
        return jar

    async def _save_cookies_from_client(self) -> None:
        """Upsert current client cookies into the database."""
        if not self._client:
            return
        cookie_list = []
        for cookie in self._client.cookies.jar:
            cookie_list.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or ".coles.com.au",
                    "path": cookie.path or "/",
                    "secure": cookie.secure,
                    "httpOnly": False,
                }
            )
        cookies_json = json.dumps(cookie_list, indent=2)
        async with async_session() as session:
            result = await session.execute(
                select(StoreCookies).where(StoreCookies.store == Store.COLES)
            )
            row = result.scalar_one_or_none()
            if row:
                row.cookies_json = cookies_json
            else:
                session.add(StoreCookies(store=Store.COLES, cookies_json=cookies_json))
            await session.commit()
        logger.info("Saved %d cookies for coles", len(cookie_list))

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the httpx client with current cookies."""
        if self._client is None or self._client.is_closed:
            cookies = await self._load_cookies()
            self._client = httpx.AsyncClient(
                base_url=COLES_BASE,
                cookies=cookies,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    **DEFAULT_HEADERS,
                },
                follow_redirects=True,
                timeout=30.0,
            )
        return self._client

    async def _request(
        self, method: str, path: str, **kwargs
    ) -> httpx.Response | None:
        """Make a request to coles.com.au, handling auth failures gracefully."""
        client = await self._get_client()
        params = kwargs.get("params", {})
        params_str = f" {params}" if params else ""
        logger.info("[Coles] → %s %s%s", method, path, params_str)
        t0 = time.perf_counter()
        try:
            resp = await client.request(method, path, **kwargs)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "[Coles] ← %d %s %s (%.0f ms)",
                resp.status_code, method, path, elapsed_ms,
            )
            if resp.status_code in (401, 403):
                try:
                    body = resp.text[:500]
                except Exception:
                    body = "<unreadable>"
                logger.warning(
                    "[Coles] Auth failure (%d) on %s %s — body: %s",
                    resp.status_code,
                    method,
                    path,
                    body,
                )
                return None
            return resp
        except httpx.HTTPError:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.exception(
                "[Coles] HTTP error on %s %s (%.0f ms)", method, path, elapsed_ms
            )
            return None

    async def _graphql(
        self, query: str, variables: dict, operation_name: str
    ) -> dict | None:
        """Execute a GraphQL operation against the Coles API."""
        client = await self._get_client()
        payload = {"query": query, "variables": variables, "operationName": operation_name}
        logger.info("[Coles] GraphQL → %s %s", operation_name, variables)
        t0 = time.perf_counter()
        try:
            resp = await client.post(
                COLES_GRAPHQL_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "[Coles] GraphQL ← %d %s (%.0f ms)", resp.status_code, operation_name, elapsed_ms
            )
            if resp.status_code in (401, 403):
                logger.warning(
                    "[Coles] GraphQL auth failure (%d) on %s", resp.status_code, operation_name
                )
                return None
            data = resp.json()
            if "errors" in data:
                logger.warning("[Coles] GraphQL errors for %s: %s", operation_name, data["errors"])
                return None
            result = data.get("data")
            logger.debug("[Coles] GraphQL %s response keys: %s", operation_name, list(result.keys()) if result else "None")
            return result
        except httpx.HTTPError:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.exception("[Coles] GraphQL HTTP error on %s (%.0f ms)", operation_name, elapsed_ms)
            return None

    # ── Auth ─────────────────────────────────────────────────────────

    async def is_authenticated(self) -> bool:
        async with async_session() as session:
            result = await session.execute(
                select(StoreCookies).where(StoreCookies.store == Store.COLES)
            )
            row = result.scalar_one_or_none()
            if not row:
                return False
            try:
                return len(json.loads(row.cookies_json)) > 0
            except Exception:
                return False

    async def import_cookies(self, cookie_json: str) -> bool:
        """Import cookies from a JSON string (e.g. from Cookie-Editor extension)."""
        try:
            raw_cookies = json.loads(cookie_json)
            if not isinstance(raw_cookies, list) or not raw_cookies:
                return False

            # Normalise to Playwright-compatible format
            normalised = []
            for c in raw_cookies:
                normalised.append(
                    {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".coles.com.au"),
                        "path": c.get("path", "/"),
                        "secure": c.get("secure", False),
                        "httpOnly": c.get("httpOnly", False),
                    }
                )

            cookies_json = json.dumps(normalised, indent=2)
            async with async_session() as session:
                result = await session.execute(
                    select(StoreCookies).where(StoreCookies.store == Store.COLES)
                )
                row = result.scalar_one_or_none()
                if row:
                    row.cookies_json = cookies_json
                else:
                    session.add(StoreCookies(store=Store.COLES, cookies_json=cookies_json))
                await session.commit()
            logger.info("Imported %d cookies for coles", len(normalised))

            # Reset client so it picks up new cookies
            if self._client and not self._client.is_closed:
                await self._client.aclose()
                self._client = None

            return True
        except Exception:
            logger.exception("Failed to import Coles cookies")
            return False

    async def login_interactive(self) -> bool:
        """Not supported for httpx-based scraper. Use import_cookies instead."""
        logger.info(
            "Interactive login not available for Coles httpx scraper. "
            "Use cookie import instead."
        )
        return False

    async def logout(self) -> None:
        async with async_session() as session:
            result = await session.execute(
                select(StoreCookies).where(StoreCookies.store == Store.COLES)
            )
            row = result.scalar_one_or_none()
            if row:
                await session.delete(row)
                await session.commit()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── Order History ────────────────────────────────────────────────

    async def get_order_history(self, limit: int = 2) -> list[ScrapedOrder]:
        orders: list[ScrapedOrder] = []
        try:
            for status in ("past", "active"):
                resp = await self._request(
                    "GET",
                    "/api/bff/orders",
                    params={"status": status, "pageNumber": 1, "pageSize": limit},
                )
                if not resp or resp.status_code != 200:
                    logger.warning(
                        "Failed to fetch Coles %s orders (status %s)",
                        status,
                        resp.status_code if resp else "no response",
                    )
                    continue

                data = resp.json()
                # Log top-level keys so we can spot unexpected response shapes
                logger.info(
                    "[Coles] Orders response top-level keys (%s): %s",
                    status,
                    list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                )
                logger.debug(
                    "[Coles] Orders raw response (%s):\n%s",
                    status,
                    json.dumps(data, indent=2, default=str)[:4000],
                )

                order_list = (
                    data.get("orders")
                    or data.get("data", {}).get("orders")
                    or (data if isinstance(data, list) else [])
                )
                logger.info("Found %d Coles %s orders", len(order_list), status)
                if order_list:
                    logger.info(
                        "[Coles] First order keys: %s",
                        list(order_list[0].keys()) if isinstance(order_list[0], dict) else order_list[0],
                    )
                    logger.debug(
                        "[Coles] First order raw:\n%s",
                        json.dumps(order_list[0], indent=2, default=str),
                    )

                for order_data in order_list:
                    order_id = str(
                        order_data.get("id")
                        or order_data.get("orderId")
                        or order_data.get("orderNumber")
                        or ""
                    )
                    if not order_id:
                        logger.warning("[Coles] Order has no id, keys: %s", list(order_data.keys()))
                        continue

                    # Fetch items for this order using the confirmed endpoint
                    items_resp = await self._request(
                        "GET", f"/api/bff/orders/{order_id}/items"
                    )
                    items: list[ScrapedOrderItem] = []
                    raw_items: list = []
                    if items_resp and items_resp.status_code == 200:
                        items_data = items_resp.json()
                        logger.info(
                            "[Coles] Items response keys for order %s: %s",
                            order_id,
                            list(items_data.keys()) if isinstance(items_data, dict) else type(items_data).__name__,
                        )
                        raw_items = (
                            items_data.get("items")
                            or items_data.get("orderItems")
                            or items_data.get("lineItems")
                            or (items_data if isinstance(items_data, list) else [])
                        )
                        logger.info(
                            "[Coles] Order %s: %d raw items found", order_id, len(raw_items)
                        )
                        if raw_items:
                            logger.info(
                                "[Coles] First item keys: %s",
                                list(raw_items[0].keys()) if isinstance(raw_items[0], dict) else raw_items[0],
                            )
                            logger.debug(
                                "[Coles] First item raw:\n%s",
                                json.dumps(raw_items[0], indent=2, default=str),
                            )
                        for item_data in raw_items:
                            item = self._parse_order_item(item_data)
                            if item:
                                items.append(item)
                            else:
                                logger.warning(
                                    "[Coles] Failed to parse item: %s",
                                    json.dumps(item_data, default=str)[:200],
                                )
                    else:
                        logger.warning(
                            "[Coles] No items response for order %s (status %s)",
                            order_id,
                            items_resp.status_code if items_resp else "no response",
                        )

                    logger.info(
                        "[Coles] Order %s: parsed %d/%d items",
                        order_id, len(items), len(raw_items) if items_resp and items_resp.status_code == 200 else 0,
                    )
                    order = self._parse_bff_order(order_data, items)
                    if order:
                        orders.append(order)
                    else:
                        logger.warning(
                            "[Coles] _parse_bff_order returned None for order_id=%s, keys=%s",
                            order_id, list(order_data.keys()),
                        )

                    if len(orders) >= limit:
                        break

            await self._save_cookies_from_client()
        except Exception:
            logger.exception("Failed to fetch Coles order history")

        return orders[:limit]

    # ── Product Search ───────────────────────────────────────────────

    async def search_product(self, query: str) -> list[ScrapedProduct]:
        products: list[ScrapedProduct] = []
        try:
            # Try GraphQL search first
            gql_data = await self._graphql(
                _GQL_SEARCH,
                {"searchTerm": query, "storeId": COLES_STORE_ID, "pageNumber": 1, "pageSize": 48},
                "SearchProducts",
            )
            if gql_data:
                raw = (gql_data.get("searchProducts") or {}).get("results") or []
                for item in raw:
                    p = self._parse_graphql_product(item)
                    if p:
                        products.append(p)
                if products:
                    logger.info("[Coles] GraphQL search returned %d products", len(products))
                    return products

            # Fallback: BFF REST search
            resp = await self._request(
                "GET",
                "/api/bff/products/search",
                params={"query": query, "page": 1, "pageSize": 20},
            )
            if not resp or resp.status_code != 200:
                resp = await self._request(
                    "GET",
                    "/api/v2/ui-api/retail-search-service/search/products",
                    params={"q": query, "pageNo": 0, "pageSize": 20, "affiliates": "coles"},
                )
            if resp and resp.status_code == 200:
                result = resp.json()
                for item in (
                    result.get("results")
                    or result.get("products")
                    or result.get("data")
                    or []
                ):
                    p = self._parse_search_result(item)
                    if p:
                        products.append(p)
        except Exception:
            logger.exception("Coles search failed for: %s", query)

        return products

    # ── Product Price ────────────────────────────────────────────────

    async def get_product_price(self, store_product_id: str) -> ScrapedProduct | None:
        try:
            # GraphQL: fetch via category search using the product id as a category hint
            # Fall through to REST if GraphQL doesn't return the specific product
            gql_data = await self._graphql(
                _GQL_CROSS_CATEGORY,
                {"categoryIds": [store_product_id], "storeId": COLES_STORE_ID},
                "GetCrossCategory",
            )
            if gql_data:
                logger.debug("[Coles] GraphQL response for product %s: %s", store_product_id, json.dumps(gql_data, indent=2, default=str)[:500])
                cross = gql_data.get("crossCategory") or {}
                products = cross.get("products") or []
                logger.info("[Coles] GraphQL returned %d products for id %s", len(products), store_product_id)
                for item in products:
                    p = self._parse_graphql_product(item)
                    if p and p.store_product_id == store_product_id:
                        return p
                # If there were products but none matched the id, return the first one
                if products:
                    p = self._parse_graphql_product(products[0])
                    if p:
                        return p

            # Fallback: REST endpoints
            resp = await self._request("GET", f"/api/products/{store_product_id}")
            if not resp or resp.status_code != 200:
                resp = await self._request("GET", f"/api/bff/products/{store_product_id}")
            if not resp or resp.status_code != 200:
                resp = await self._request("GET", f"/api/v2/ui-api/product/{store_product_id}")

            if resp and resp.status_code == 200:
                data = resp.json()
                product_data = data.get("product") or data.get("Product") or data
                return self._parse_search_result(product_data)
        except Exception:
            logger.exception("Coles price fetch failed for: %s", store_product_id)
        return None

    async def get_products_by_category(
        self, category_ids: list[str], memory_token: str | None = None
    ) -> tuple[list[ScrapedProduct], str | None]:
        """Fetch products for given category IDs via GetCrossCategory GraphQL.

        Returns (products, next_memory_token). Pass the returned memory_token
        back on subsequent calls to page through results.
        """
        products: list[ScrapedProduct] = []
        next_token: str | None = None
        try:
            variables: dict = {"categoryIds": category_ids, "storeId": COLES_STORE_ID}
            if memory_token:
                variables["memoryToken"] = memory_token
            gql_data = await self._graphql(_GQL_CROSS_CATEGORY, variables, "GetCrossCategory")
            if gql_data:
                cross = gql_data.get("crossCategory") or {}
                next_token = cross.get("memoryToken")
                for item in cross.get("products") or []:
                    p = self._parse_graphql_product(item)
                    if p:
                        products.append(p)
                logger.info(
                    "[Coles] GetCrossCategory returned %d products (next_token=%s)",
                    len(products),
                    bool(next_token),
                )
        except Exception:
            logger.exception("Coles get_products_by_category failed for: %s", category_ids)
        return products, next_token

    # ── Add to Cart ──────────────────────────────────────────────────

    async def add_to_cart(self, items: list[tuple[str, int]]) -> bool:
        try:
            for product_id, quantity in items:
                resp = await self._request(
                    "POST",
                    "/api/bff/cart/items",
                    json={"productId": product_id, "quantity": quantity},
                )
                if not resp or resp.status_code not in (200, 201):
                    # Fallback to legacy cart endpoint
                    resp = await self._request(
                        "POST",
                        "/api/cart/items",
                        json={"productId": product_id, "quantity": quantity},
                    )
                if not resp or resp.status_code not in (200, 201):
                    logger.warning(
                        "Failed to add Coles product %s to cart", product_id
                    )
                    return False

            await self._save_cookies_from_client()
            return True
        except Exception:
            logger.exception("Coles add to cart failed")
            return False

    async def get_cart_url(self) -> str:
        return f"{COLES_BASE}/cart"

    # ── Parsing helpers ──────────────────────────────────────────────

    def _parse_bff_order(
        self, data: dict, items: list[ScrapedOrderItem]
    ) -> ScrapedOrder | None:
        """Parse an order from the Coles BFF orders response."""
        try:
            order_id = str(
                data.get("orderId")
                or data.get("id")
                or data.get("orderNumber")
                or ""
            )
            if not order_id:
                return None

            date_str = (
                data.get("orderPlacementTime")
                or data.get("orderDate")
                or data.get("deliveryDate")
                or data.get("createdDate")
                or data.get("date")
                or ""
            )
            try:
                order_date = datetime.fromisoformat(
                    date_str.replace("Z", "+00:00")
                ).date()
            except (ValueError, AttributeError):
                order_date = datetime.now().date()

            attrs = data.get("orderAttributes") or {}
            total = float(
                attrs.get("orderTotalPrice")
                or data.get("totalAmount")
                or data.get("total")
                or data.get("orderTotal")
                or 0
            )

            return ScrapedOrder(
                store_order_id=order_id,
                order_date=order_date,
                total_amount=total,
                status=data.get("orderStatus") or data.get("status"),
                items=items,
            )
        except Exception:
            logger.debug("Failed to parse Coles BFF order", exc_info=True)
            return None

    def _parse_order_item(self, data: dict) -> ScrapedOrderItem | None:
        """Parse a single order item from the BFF items response."""
        try:
            product_id = str(
                data.get("productId")
                or data.get("sku")
                or data.get("id")
                or data.get("stockcode")
                or ""
            )
            if not product_id:
                return None

            product = data.get("product") or {}
            image_uri = (product.get("imageUris") or [{}])[0].get("uri") or ""
            image_url = (
                f"https://productimages.coles.com.au/productimages/1-E1{image_uri}"
                if image_uri else None
            )

            return ScrapedOrderItem(
                store_product_id=product_id,
                name=(
                    product.get("name")
                    or data.get("name")
                    or data.get("productName")
                    or data.get("displayName")
                    or "Unknown"
                ),
                quantity=int(data.get("quantity") or data.get("qty") or 1),
                price_paid=float(
                    data.get("itemTotalPrice")
                    or data.get("price")
                    or data.get("totalPrice")
                    or data.get("salePrice")
                    or data.get("unitPrice")
                    or 0
                ),
                brand=product.get("brand") or data.get("brand"),
                unit_size=(
                    product.get("size")
                    or data.get("size")
                    or data.get("packageSize")
                    or data.get("unitSize")
                ),
                image_url=image_url or data.get("imageUrl") or data.get("image"),
            )
        except Exception:
            logger.debug("Failed to parse Coles order item", exc_info=True)
            return None

    def _parse_graphql_product(self, data: dict) -> ScrapedProduct | None:
        """Parse a product from a GraphQL response (pricing.now / pricing.unit.price)."""
        try:
            pricing = data.get("pricing") or {}
            unit = pricing.get("unit") or {}
            image_uris = data.get("imageUris") or []
            image_uri = image_uris[0].get("uri") if image_uris else None
            image_url = (
                f"https://productimages.coles.com.au/productimages/1-E1{image_uri}"
                if image_uri else None
            )
            product_id = str(data.get("id") or "")
            name = data.get("name") or ""
            if not product_id or not name:
                return None
            return ScrapedProduct(
                store_product_id=product_id,
                name=name,
                current_price=float(pricing.get("now") or 0),
                brand=data.get("brand"),
                category=None,
                unit_size=data.get("size"),
                unit_price=float(unit.get("price") or 0) or None,
                unit_price_measure=None,
                image_url=image_url,
                product_url=f"{COLES_BASE}/product/{name.lower().replace(' ', '-')}-{product_id}",
                is_available=True,
            )
        except Exception:
            logger.debug("Failed to parse Coles GraphQL product", exc_info=True)
            return None

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


coles_scraper = ColesScraper()
