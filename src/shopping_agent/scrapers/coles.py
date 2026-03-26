import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from datetime import datetime

import httpx
from sqlalchemy import select

from ..config import COLES_PRICE_FETCH_DELAY_S, settings
from ..database import async_session
from ..models.product import Store
from ..models.store_cookies import StoreCookies
from .base import BaseScraper, ScrapedOrder, ScrapedOrderItem, ScrapedProduct
from .coles_queries import GQL_CROSS_CATEGORY

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
    "Ocp-Apim-Subscription-Key": settings.coles_api_key or "",
    "dsch-channel": "coles.online.1site.desktop",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


_COLES_UNAVAILABLE_STATES = {"UNAVAILABLE", "IN_STORE_ONLY", "NOT_AVAILABLE", "OUT_OF_STOCK"}


def _coles_is_available(data: dict) -> bool:
    """Return False if the Coles BFF product data indicates it's unavailable online."""
    availability = data.get("availability")
    if isinstance(availability, str) and availability.upper() in _COLES_UNAVAILABLE_STATES:
        return False
    if isinstance(availability, bool):
        return availability
    # Some responses use an onlineHeadline or restriction field
    if data.get("restriction") == "NOT_FOR_SALE":
        return False
    return True


class ColesScraper(BaseScraper):
    store = Store.COLES
    _cookie_domain: str = ".coles.com.au"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._bare_client: httpx.AsyncClient | None = None
        self._next_build_id: str | None = None
        self._build_id_lock: asyncio.Lock = asyncio.Lock()

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
        """Return True if Coles cookies are stored in the database."""
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
                logger.warning("Failed to parse stored Coles cookies JSON", exc_info=True)
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

    async def validate_cookies(self) -> dict:
        """Make a real API call to verify the stored cookies actually work.
        Returns {"ok": bool, "detail": str}.
        """
        if not await self.is_authenticated():
            return {"ok": False, "detail": "No cookies stored"}
        resp = await self._request(
            "GET", "/api/bff/orders", params={"status": "past", "pageNumber": 1, "pageSize": 1}
        )
        if resp is None:
            return {"ok": False, "detail": "API returned 401/403 — cookies expired or invalid"}
        if resp.status_code == 200:
            return {"ok": True, "detail": f"API reachable (HTTP {resp.status_code})"}
        return {"ok": False, "detail": f"Unexpected response: HTTP {resp.status_code}"}

    async def login_interactive(self) -> bool:
        """Not supported for httpx-based scraper. Use import_cookies instead."""
        logger.info(
            "Interactive login not available for Coles httpx scraper. "
            "Use cookie import instead."
        )
        return False

    async def logout(self) -> None:
        """Delete stored Coles cookies and close the HTTP client."""
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

    async def get_order_history(self, limit: int = 10) -> list[ScrapedOrder]:
        """Fetch up to `limit` past online and in-store orders from Coles.

        Retrieves both "past"/"active" online orders and in-store orders
        from the Coles BFF API, fetching item details for each order.

        Args:
            limit: Maximum number of online orders and in-store orders to fetch each.

        Returns:
            List of ScrapedOrder objects with items populated.
        """
        PAGE_SIZE = 20
        orders: list[ScrapedOrder] = []
        online_count = 0
        instore_count = 0
        try:
            for status in ("past", "active"):
                page = 1
                while online_count < limit:
                    resp = await self._request(
                        "GET",
                        "/api/bff/orders",
                        params={"status": status, "pageNumber": page, "pageSize": PAGE_SIZE},
                    )
                    if not resp or resp.status_code != 200:
                        logger.warning(
                            "Failed to fetch Coles %s orders page %d (status %s)",
                            status, page,
                            resp.status_code if resp else "no response",
                        )
                        break

                    data = resp.json()
                    logger.info(
                        "[Coles] Orders response top-level keys (%s, page %d): %s",
                        status, page,
                        list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                    )
                    logger.debug(
                        "[Coles] Orders raw response (%s, page %d):\n%s",
                        status, page,
                        json.dumps(data, indent=2, default=str)[:4000],
                    )

                    order_list = (
                        data.get("orders")
                        or data.get("data", {}).get("orders")
                        or (data if isinstance(data, list) else [])
                    )
                    logger.info("Found %d Coles %s orders on page %d", len(order_list), status, page)

                    if not order_list:
                        break

                    if page == 1 and order_list:
                        logger.info(
                            "[Coles] First order keys: %s",
                            list(order_list[0].keys()) if isinstance(order_list[0], dict) else order_list[0],
                        )

                    for order_data in order_list:
                        if online_count >= limit:
                            break

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
                            online_count += 1
                        else:
                            logger.warning(
                                "[Coles] _parse_bff_order returned None for order_id=%s, keys=%s",
                                order_id, list(order_data.keys()),
                            )

                    # If page returned fewer than PAGE_SIZE orders, we've hit the last page
                    if len(order_list) < PAGE_SIZE:
                        break

                    page += 1

            # ── In-store purchases ────────────────────────────────────
            page = 1
            while instore_count < limit:
                resp = await self._request(
                    "GET",
                    "/api/bff/orders",
                    params={"status": "in-store", "pageNumber": page, "pageSize": PAGE_SIZE},
                )
                if not resp or resp.status_code != 200:
                    logger.warning("Failed to fetch Coles in-store orders page %d (status %s)",
                                   page, resp.status_code if resp else "no response")
                    break

                data = resp.json()
                order_list = (
                    data.get("orders")
                    or data.get("data", {}).get("orders")
                    or (data if isinstance(data, list) else [])
                )
                logger.info("Found %d Coles in-store orders on page %d", len(order_list), page)

                if not order_list:
                    break

                for order_data in order_list:
                    if instore_count >= limit:
                        break

                    order_id = str(order_data.get("orderId") or order_data.get("id") or "")
                    txn_id = str(order_data.get("transactionId") or order_data.get("transactionBarcode") or "")
                    if not order_id or not txn_id:
                        continue

                    # Fetch order detail (includes items) using the v2 endpoint
                    detail_resp = await self._request(
                        "GET", f"/api/bff/orders/{order_id}",
                        headers={"x-api-version": "2", "x-transaction-id": txn_id},
                    )
                    items: list[ScrapedOrderItem] = []
                    if detail_resp and detail_resp.status_code == 200:
                        detail = detail_resp.json()
                        for item_data in detail.get("items", []):
                            item = self._parse_instore_item(item_data)
                            if item:
                                items.append(item)
                    else:
                        logger.warning("[Coles] Could not fetch in-store order detail for %s", order_id)

                    # Use transactionId as the unique order key to avoid collisions with online IDs
                    instore_data = dict(order_data)
                    instore_data["orderId"] = f"instore-{txn_id}"
                    order = self._parse_bff_order(instore_data, items)
                    if order:
                        orders.append(order)
                        instore_count += 1

                if len(order_list) < PAGE_SIZE:
                    break
                page += 1

            await self._save_cookies_from_client()
        except Exception:
            logger.exception("Failed to fetch Coles order history")

        return orders

    async def stream_order_history(self, limit: int = 10) -> AsyncGenerator[ScrapedOrder, None]:
        """Yield ScrapedOrder one at a time as each order's items are fetched."""
        PAGE_SIZE = 20
        online_count = 0
        instore_count = 0
        try:
            for status in ("past", "active"):
                page = 1
                while online_count < limit:
                    resp = await self._request(
                        "GET", "/api/bff/orders",
                        params={"status": status, "pageNumber": page, "pageSize": PAGE_SIZE},
                    )
                    if not resp or resp.status_code != 200:
                        break
                    data = resp.json()
                    order_list = (
                        data.get("orders")
                        or data.get("data", {}).get("orders")
                        or (data if isinstance(data, list) else [])
                    )
                    if not order_list:
                        break
                    for order_data in order_list:
                        if online_count >= limit:
                            break
                        order_id = str(
                            order_data.get("id") or order_data.get("orderId")
                            or order_data.get("orderNumber") or ""
                        )
                        if not order_id:
                            continue
                        items_resp = await self._request("GET", f"/api/bff/orders/{order_id}/items")
                        items = []
                        if items_resp and items_resp.status_code == 200:
                            items_data = items_resp.json()
                            raw_items = (
                                items_data.get("items") or items_data.get("orderItems")
                                or items_data.get("lineItems")
                                or (items_data if isinstance(items_data, list) else [])
                            )
                            for item_data in raw_items:
                                item = self._parse_order_item(item_data)
                                if item:
                                    items.append(item)
                        order = self._parse_bff_order(order_data, items)
                        if order:
                            yield order
                            online_count += 1
                    if len(order_list) < PAGE_SIZE:
                        break
                    page += 1

            page = 1
            while instore_count < limit:
                resp = await self._request(
                    "GET", "/api/bff/orders",
                    params={"status": "in-store", "pageNumber": page, "pageSize": PAGE_SIZE},
                )
                if not resp or resp.status_code != 200:
                    break
                data = resp.json()
                order_list = (
                    data.get("orders") or data.get("data", {}).get("orders")
                    or (data if isinstance(data, list) else [])
                )
                if not order_list:
                    break
                for order_data in order_list:
                    if instore_count >= limit:
                        break
                    order_id = str(order_data.get("orderId") or order_data.get("id") or "")
                    txn_id = str(order_data.get("transactionId") or order_data.get("transactionBarcode") or "")
                    if not order_id or not txn_id:
                        continue
                    detail_resp = await self._request(
                        "GET", f"/api/bff/orders/{order_id}",
                        headers={"x-api-version": "2", "x-transaction-id": txn_id},
                    )
                    items = []
                    if detail_resp and detail_resp.status_code == 200:
                        detail = detail_resp.json()
                        for item_data in detail.get("items", []):
                            item = self._parse_instore_item(item_data)
                            if item:
                                items.append(item)
                    instore_data = dict(order_data)
                    instore_data["orderId"] = f"instore-{txn_id}"
                    order = self._parse_bff_order(instore_data, items)
                    if order:
                        yield order
                        instore_count += 1
                if len(order_list) < PAGE_SIZE:
                    break
                page += 1

            await self._save_cookies_from_client()
        except Exception:
            logger.exception("Failed to stream Coles order history")

    # ── Product Search ───────────────────────────────────────────────

    async def search_product(self, query: str) -> list[ScrapedProduct]:
        """Search Coles for products matching the query via the BFF search endpoint.

        Args:
            query: Free-text search query.

        Returns:
            List of ScrapedProduct results from the Coles BFF search API.
        """
        products: list[ScrapedProduct] = []
        try:
            # Use the bare client BFF search — same approach as get_product_price
            # which is proven to work without triggering bot-detection.
            if self._bare_client is None or self._bare_client.is_closed:
                self._bare_client = httpx.AsyncClient(
                    base_url=COLES_BASE,
                    headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
                    follow_redirects=True,
                    timeout=15.0,
                )
            resp = await self._bare_client.get(
                "/api/bff/products/search",
                params={
                    "searchTerm": query,
                    "subscription-key": settings.coles_api_key or "",
                    "storeId": COLES_STORE_ID.split(":")[-1],
                    "start": 0,
                    "pageSize": 24,
                },
            )
            if resp and resp.status_code == 200:
                for item in resp.json().get("results") or []:
                    p = self._parse_graphql_product(item)
                    if p:
                        products.append(p)
                logger.info("[Coles] BFF search returned %d products for %r", len(products), query)
        except Exception:
            logger.exception("Coles search failed for: %s", query)

        return products

    # ── Product Price ────────────────────────────────────────────────

    async def _get_next_build_id(self) -> str | None:
        """Fetch the current Next.js build ID from the Coles homepage."""
        if self._next_build_id:
            return self._next_build_id
        async with self._build_id_lock:
            # Double-check after acquiring lock — another coroutine may have fetched it
            if self._next_build_id:
                return self._next_build_id
            try:
                import re
                # Use a cookie-free client — the authenticated client gets an Incapsula
                # bot challenge page when the reese84 session cookie expires.
                if self._bare_client is None or self._bare_client.is_closed:
                    self._bare_client = httpx.AsyncClient(
                        base_url=COLES_BASE,
                        headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,*/*"},
                        follow_redirects=True,
                        timeout=15.0,
                    )
                resp = await self._bare_client.get("/")
                if resp and resp.status_code == 200:
                    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', resp.text)
                    if m:
                        self._next_build_id = m.group(1)
                        logger.info("[Coles] Next.js build ID: %s", self._next_build_id)
                        return self._next_build_id
                    logger.warning("[Coles] buildId not found in homepage (%d bytes)", len(resp.text))
            except Exception:
                logger.debug("[Coles] Failed to get Next.js build ID", exc_info=True)
        return None

    async def get_product_price(self, store_product_id: str, product_name: str | None = None) -> ScrapedProduct | None:
        """Fetch the current price for a specific Coles product.

        Searches the BFF API by product name (or ID as fallback) and returns
        the matching product entry for the given store_product_id.

        Args:
            store_product_id: Coles product ID to look up.
            product_name: Optional product name to use as the search term.

        Returns:
            ScrapedProduct with current price, or None if not found.
        """
        try:
            if COLES_PRICE_FETCH_DELAY_S:
                await asyncio.sleep(COLES_PRICE_FETCH_DELAY_S)
            if self._bare_client is None or self._bare_client.is_closed:
                self._bare_client = httpx.AsyncClient(
                    base_url=COLES_BASE,
                    headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
                    follow_redirects=True,
                    timeout=15.0,
                )
            search_term = product_name or store_product_id
            resp = await self._bare_client.get(
                "/api/bff/products/search",
                params={
                    "searchTerm": search_term,
                    "subscription-key": settings.coles_api_key or "",
                    "storeId": COLES_STORE_ID.split(":")[-1],
                    "start": 0,
                },
            )
            if resp and resp.status_code == 200:
                results = resp.json().get("results") or []
                for item in results:
                    p = self._parse_graphql_product(item)
                    if p and p.store_product_id == store_product_id:
                        logger.debug(
                            "[Coles price] %s raw fields: availability=%s pricing=%s",
                            store_product_id,
                            item.get("availability"),
                            item.get("pricing"),
                        )
                        return p
                # Search succeeded but product not in results — treat as unavailable/delisted
                logger.info(
                    "[Coles price] %s not found in search results (delisted/unavailable)",
                    store_product_id,
                )
                return ScrapedProduct(
                    store_product_id=store_product_id,
                    name=product_name or store_product_id,
                    current_price=0,
                    is_available=False,
                )
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
            gql_data = await self._graphql(GQL_CROSS_CATEGORY, variables, "GetCrossCategory")
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

    async def add_to_cart(self, items: list[tuple[str, int]]) -> dict[str, bool]:
        """Add items to the Coles cart via the BFF trolley API.

        Args:
            items: List of (store_product_id, quantity) tuples to add.

        Returns:
            Dict mapping store_product_id to True if added successfully, False otherwise.
        """
        store_num = COLES_STORE_ID.split(":")[-1]
        endpoint = f"/api/bff/trolley/store/{store_num}/items"
        results: dict[str, bool] = {}
        try:
            for product_id, quantity in items:
                try:
                    resp = await self._request(
                        "POST",
                        endpoint,
                        json={"items": [{"productId": int(product_id), "quantity": quantity}]},
                    )
                    if resp and resp.status_code in (200, 201):
                        body = resp.text
                        logger.info(
                            "Coles add-to-cart product=%s status=%d body=%s",
                            product_id,
                            resp.status_code,
                            body[:800],
                        )
                        # Verify the item was actually added by checking the response body.
                        # The BFF may return 200 but silently reject items (e.g. unavailable,
                        # in-store only). Check for unavailableItems or missing from trolleyItems.
                        success = True
                        try:
                            data = resp.json()
                            unavailable = data.get("unavailableItems") or data.get("unavailable") or []
                            if unavailable:
                                unavailable_ids = {
                                    str(u.get("productId") or u.get("stockcode") or u.get("id") or "")
                                    for u in unavailable
                                }
                                if str(product_id) in unavailable_ids:
                                    logger.warning(
                                        "Coles product %s returned 200 but is in unavailableItems",
                                        product_id,
                                    )
                                    success = False
                            # If response has trolleyItems/items, check our product is present
                            if success:
                                trolley_items = (
                                    data.get("trolleyItems")
                                    or data.get("items")
                                    or data.get("trolley", {}).get("items")
                                    or []
                                )
                                if trolley_items:
                                    added_ids = {
                                        str(t.get("productId") or t.get("stockcode") or t.get("id") or "")
                                        for t in trolley_items
                                    }
                                    if str(product_id) not in added_ids:
                                        logger.warning(
                                            "Coles product %s returned 200 but not found in trolleyItems (ids: %s)",
                                            product_id,
                                            list(added_ids)[:10],
                                        )
                                        success = False
                        except Exception:
                            logger.debug("Could not parse Coles add-to-cart response body for product %s", product_id)
                        results[str(product_id)] = success
                    else:
                        logger.warning(
                            "Failed to add Coles product %s to cart (status=%s body=%s)",
                            product_id,
                            resp.status_code if resp else None,
                            resp.text[:300] if resp else None,
                        )
                        results[str(product_id)] = False
                except Exception:
                    logger.exception("Coles add to cart failed for product %s", product_id)
                    results[str(product_id)] = False
            await self._save_cookies_from_client()
        except Exception:
            logger.exception("Coles add to cart failed")
            for product_id, _ in items:
                if str(product_id) not in results:
                    results[str(product_id)] = False
        return results

    async def get_cart_url(self) -> str:
        """Return the Coles homepage URL for the user to review/submit their cart."""
        return COLES_BASE

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

            store_obj = data.get("store") or {}
            store_name = data.get("storeName") or store_obj.get("suburb") or None
            store_id = store_obj.get("storeId") or None

            return ScrapedOrder(
                store_order_id=order_id,
                order_date=order_date,
                total_amount=total,
                status=data.get("orderStatus") or data.get("status"),
                items=items,
                store_name=store_name,
                store_id=store_id,
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
                f"https://cdn.productimages.coles.com.au/productimages{image_uri}"
                if image_uri else None
            )

            quantity = int(data.get("quantity") or data.get("qty") or 1)
            # Prefer unit price fields; fall back to dividing the line total by quantity
            item_total = float(data.get("itemTotalPrice") or data.get("totalPrice") or 0)
            unit_price = float(
                data.get("salePrice")
                or data.get("unitPrice")
                or data.get("price")
                or (item_total / quantity if item_total and quantity else 0)
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
                quantity=quantity,
                price_paid=unit_price,
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

    def _parse_instore_item(self, data: dict) -> ScrapedOrderItem | None:
        """Parse a single in-store order item from the getOrderV2 detail response.

        In-store items nest quantity/price inside an 'orderItem' sub-object.
        """
        try:
            product_id = str(data.get("id") or data.get("productId") or "")
            if not product_id:
                return None

            order_item = data.get("orderItem") or {}
            quantity = int(order_item.get("quantity") or 1)
            unit_price = float(
                order_item.get("unitPrice")
                or order_item.get("salePrice")
                or (order_item.get("itemTotalPrice", 0) / quantity if quantity else 0)
            )

            image_uris = data.get("imageUris") or []
            image_uri = image_uris[0].get("uri") if image_uris else None
            image_url = (
                f"https://cdn.productimages.coles.com.au/productimages{image_uri}"
                if image_uri else None
            )

            return ScrapedOrderItem(
                store_product_id=product_id,
                name=data.get("name") or "Unknown",
                quantity=quantity,
                price_paid=unit_price,
                brand=data.get("brand") or None,
                unit_size=data.get("size") or None,
                image_url=image_url,
            )
        except Exception:
            logger.debug("Failed to parse Coles in-store item", exc_info=True)
            return None

    def _parse_graphql_product(self, data: dict) -> ScrapedProduct | None:
        """Parse a product from a GraphQL response (pricing.now / pricing.unit.price)."""
        try:
            pricing = data.get("pricing") or {}
            unit = pricing.get("unit") or {}
            image_uris = data.get("imageUris") or []
            image_uri = image_uris[0].get("uri") if image_uris else None
            image_url = (
                f"https://cdn.productimages.coles.com.au/productimages{image_uri}"
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
                is_available=_coles_is_available(data),
            )
        except Exception:
            logger.debug("Failed to parse Coles GraphQL product", exc_info=True)
            return None

    def _parse_search_result(self, data: dict) -> ScrapedProduct | None:
        """Parse a product from a generic search result dict."""
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
            logger.debug("Failed to parse Coles search result", exc_info=True)
            return None


# The singleton instance lives in scrapers.registry to avoid circular imports.
# Legacy code that imports `coles_scraper` from this module will break; update
# those call-sites to use `from ..scrapers.registry import coles_scraper`.
