import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import AsyncGenerator

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ScrapedOrderItem:
    store_product_id: str
    name: str
    quantity: int
    price_paid: float
    brand: str | None = None
    unit_size: str | None = None
    image_url: str | None = None
    category: str | None = None


@dataclass
class ScrapedOrder:
    store_order_id: str
    order_date: date
    items: list[ScrapedOrderItem] = field(default_factory=list)
    total_amount: float | None = None
    status: str | None = None
    store_name: str | None = None
    store_id: str | None = None


@dataclass
class ScrapedProduct:
    store_product_id: str
    name: str
    current_price: float
    is_available: bool = True
    brand: str | None = None
    category: str | None = None
    unit_size: str | None = None
    unit_price: float | None = None
    unit_price_measure: str | None = None
    image_url: str | None = None
    product_url: str | None = None


class BaseScraper(ABC):
    #: Default cookie domain for this store (e.g. ".coles.com.au").
    _cookie_domain: str = ""

    @abstractmethod
    async def is_authenticated(self) -> bool:
        """Return True if valid credentials/cookies are stored for this store."""
        ...

    @abstractmethod
    async def login_interactive(self) -> bool:
        """Open a visible browser window for the user to log in manually."""
        ...

    @abstractmethod
    async def get_order_history(self, limit: int = 10) -> list[ScrapedOrder]:
        """Fetch up to `limit` past orders from this store.

        Args:
            limit: Maximum number of orders to return.

        Returns:
            List of ScrapedOrder objects with items populated.
        """
        ...

    async def stream_order_history(self, limit: int = 10) -> AsyncGenerator[ScrapedOrder, None]:
        """Yield orders one at a time. Default implementation wraps get_order_history."""
        for order in await self.get_order_history(limit=limit):
            yield order

    @abstractmethod
    async def search_product(self, query: str) -> list[ScrapedProduct]:
        """Search for products matching the given query string.

        Args:
            query: Free-text search query (e.g. product name or brand + name).

        Returns:
            List of ScrapedProduct results ordered by store relevance.
        """
        ...

    @abstractmethod
    async def get_product_price(self, store_product_id: str, product_name: str | None = None) -> ScrapedProduct | None:
        """Fetch current price and availability for a specific product.

        Args:
            store_product_id: Store-specific product identifier.
            product_name: Optional product name to aid search-based lookups.

        Returns:
            ScrapedProduct with current price, or None if not found.
        """
        ...

    @abstractmethod
    async def add_to_cart(self, items: list[tuple[str, int]]) -> dict[str, bool]:
        """Add items to cart. items = [(store_product_id, quantity), ...]
        Returns a dict of {store_product_id: success}."""
        ...

    @abstractmethod
    async def get_cart_url(self) -> str:
        """Return URL for user to review/submit the cart."""
        ...

    async def import_cookies(self, cookie_json: str) -> bool:
        """Import cookies from JSON string. Override in subclasses that support it."""
        return False

    async def logout(self) -> None:
        """Clear stored auth. Override in subclasses."""
        pass

    async def _load_cookies(self) -> httpx.Cookies:
        """Load persisted cookies for this store from the database.

        Returns:
            An httpx.Cookies jar populated with the stored cookies.
        """
        from ..database import async_session
        from ..models.store_cookies import StoreCookies
        from sqlalchemy import select

        jar = httpx.Cookies()
        async with async_session() as session:
            result = await session.execute(
                select(StoreCookies).where(StoreCookies.store == self.store)
            )
            row = result.scalar_one_or_none()
            if row:
                try:
                    raw_cookies = json.loads(row.cookies_json)
                    for c in raw_cookies:
                        jar.set(
                            c["name"],
                            c["value"],
                            domain=c.get("domain", self._cookie_domain),
                            path=c.get("path", "/"),
                        )
                    logger.info(
                        "Loaded %d cookies for %s",
                        len(raw_cookies),
                        self.store.value,
                    )
                except Exception:
                    logger.warning(
                        "Failed to load %s cookies", self.store.value, exc_info=True
                    )
        return jar

    async def _save_cookies_from_client(self) -> None:
        """Persist current client cookies for this store to the database.

        Reads cookies from `self._client`. Subclasses must set `self._client`
        before calling this method. Does nothing if `self._client` is None.
        """
        from ..database import async_session
        from ..models.store_cookies import StoreCookies
        from sqlalchemy import select

        client = getattr(self, "_client", None)
        if not client:
            return

        cookie_list = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or self._cookie_domain,
                "path": cookie.path or "/",
                "secure": cookie.secure,
                "httpOnly": False,
            }
            for cookie in client.cookies.jar
        ]
        cookies_json = json.dumps(cookie_list, indent=2)

        async with async_session() as session:
            result = await session.execute(
                select(StoreCookies).where(StoreCookies.store == self.store)
            )
            row = result.scalar_one_or_none()
            if row:
                row.cookies_json = cookies_json
            else:
                session.add(StoreCookies(store=self.store, cookies_json=cookies_json))
            await session.commit()

        logger.info("Saved %d cookies for %s", len(cookie_list), self.store.value)
