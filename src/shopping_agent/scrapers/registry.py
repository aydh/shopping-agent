"""Central registry for scraper singletons and per-user scraper instances.

All application code should import scrapers from here rather than from the
individual scraper modules.  This ensures that every caller shares the same
httpx client and cookie state.

Usage::

    from .scrapers.registry import get_scraper, coles_scraper, woolworths_scraper
    from ..models.product import Store
    import uuid

    # Global (legacy) singletons — no user context:
    scraper = coles_scraper

    # Per-user instance (preferred for multi-user flows):
    scraper = get_scraper(user_id, Store.COLES)
"""

import uuid

from ..models.product import Store
from .base import BaseScraper
from .coles import ColesScraper
from .woolworths import WoolworthsScraper

#: Singleton Coles scraper shared across the whole application (no user context).
coles_scraper: ColesScraper = ColesScraper()

#: Singleton Woolworths scraper shared across the whole application (no user context).
woolworths_scraper: WoolworthsScraper = WoolworthsScraper()

_REGISTRY: dict[tuple[uuid.UUID | None, Store], BaseScraper] = {
    (None, Store.COLES): coles_scraper,
    (None, Store.WOOLWORTHS): woolworths_scraper,
}

_registry_lock_available = False
try:
    import importlib.util
    _registry_lock_available = importlib.util.find_spec("asyncio") is not None
except ImportError:
    pass


def get_scraper(store_or_user_id: "Store | uuid.UUID", store: "Store | None" = None) -> BaseScraper:
    """Return a scraper instance for the given store (and optional user).

    Can be called in two ways:

    1. ``get_scraper(Store.COLES)`` — returns the global singleton (no user context).
    2. ``get_scraper(user_id, Store.COLES)`` — returns (or creates) a per-user instance.

    Args:
        store_or_user_id: Either a :class:`Store` value (legacy single-arg form) or a
            :class:`uuid.UUID` identifying the current user.
        store: The :class:`Store` to look up.  Required when *store_or_user_id* is a UUID.

    Returns:
        The shared :class:`BaseScraper` instance for that (user, store) combination.

    Raises:
        KeyError: If *store* has no registered scraper class.
        TypeError: If arguments are invalid.
    """
    # Legacy single-argument form: get_scraper(Store.COLES)
    if isinstance(store_or_user_id, Store):
        user_id: uuid.UUID | None = None
        resolved_store: Store = store_or_user_id
    elif isinstance(store_or_user_id, uuid.UUID):
        if store is None:
            raise TypeError("store argument is required when user_id is provided")
        user_id = store_or_user_id
        resolved_store = store
    else:
        raise TypeError(f"Expected Store or UUID, got {type(store_or_user_id).__name__}")

    key = (user_id, resolved_store)
    if key not in _REGISTRY:
        scraper = _create_scraper(resolved_store, user_id)
        _REGISTRY[key] = scraper
    return _REGISTRY[key]


def _create_scraper(store: Store, user_id: uuid.UUID | None) -> BaseScraper:
    """Instantiate a new scraper for the given store and user."""
    if store == Store.COLES:
        return ColesScraper(user_id=user_id)
    if store == Store.WOOLWORTHS:
        return WoolworthsScraper(user_id=user_id)
    raise KeyError(f"No scraper registered for store {store!r}")
