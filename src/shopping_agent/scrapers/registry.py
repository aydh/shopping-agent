"""Central registry for scraper singletons.

All application code should import scrapers from here rather than from the
individual scraper modules.  This ensures that every caller shares the same
httpx client and cookie state.

Usage::

    from .scrapers.registry import get_scraper, coles_scraper, woolworths_scraper
    from ..models.product import Store

    scraper = get_scraper(Store.COLES)
"""

from ..models.product import Store
from .base import BaseScraper
from .coles import ColesScraper
from .woolworths import WoolworthsScraper

#: Singleton Coles scraper shared across the whole application.
coles_scraper: ColesScraper = ColesScraper()

#: Singleton Woolworths scraper shared across the whole application.
woolworths_scraper: WoolworthsScraper = WoolworthsScraper()

_REGISTRY: dict[Store, BaseScraper] = {
    Store.COLES: coles_scraper,
    Store.WOOLWORTHS: woolworths_scraper,
}


def get_scraper(store: Store) -> BaseScraper:
    """Return the singleton scraper for *store*.

    Args:
        store: The store enum value to look up.

    Returns:
        The shared :class:`BaseScraper` instance for that store.

    Raises:
        KeyError: If *store* has no registered scraper.
    """
    return _REGISTRY[store]
