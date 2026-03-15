from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date


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
    @abstractmethod
    async def is_authenticated(self) -> bool: ...

    @abstractmethod
    async def login_interactive(self) -> bool:
        """Open a visible browser window for the user to log in manually."""
        ...

    @abstractmethod
    async def get_order_history(self, limit: int = 10) -> list[ScrapedOrder]: ...

    @abstractmethod
    async def search_product(self, query: str) -> list[ScrapedProduct]: ...

    @abstractmethod
    async def get_product_price(self, store_product_id: str, product_name: str | None = None) -> ScrapedProduct | None: ...

    @abstractmethod
    async def add_to_cart(self, items: list[tuple[str, int]]) -> bool:
        """Add items to cart. items = [(store_product_id, quantity), ...]"""
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
