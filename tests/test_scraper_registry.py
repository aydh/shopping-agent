"""Tests for scrapers/registry.py: get_scraper and _create_scraper."""
from __future__ import annotations

import uuid

import pytest

from shopping_agent.models.product import Store
from shopping_agent.scrapers.coles import ColesScraper
from shopping_agent.scrapers.registry import _REGISTRY, _create_scraper, get_scraper
from shopping_agent.scrapers.woolworths import WoolworthsScraper


def test_get_scraper_single_arg_coles_returns_singleton():
    s = get_scraper(Store.COLES)
    assert isinstance(s, ColesScraper)
    assert s is get_scraper(Store.COLES)  # same object (cached)


def test_get_scraper_single_arg_woolworths_returns_singleton():
    s = get_scraper(Store.WOOLWORTHS)
    assert isinstance(s, WoolworthsScraper)


def test_get_scraper_with_user_id_creates_per_user_instance():
    user_id = uuid.uuid4()
    s = get_scraper(user_id, Store.COLES)
    assert isinstance(s, ColesScraper)
    # Second call with same id returns cached instance
    assert s is get_scraper(user_id, Store.COLES)


def test_get_scraper_different_users_get_different_instances():
    uid1, uid2 = uuid.uuid4(), uuid.uuid4()
    assert get_scraper(uid1, Store.COLES) is not get_scraper(uid2, Store.COLES)


def test_get_scraper_user_id_without_store_raises():
    with pytest.raises(TypeError, match="store argument is required"):
        get_scraper(uuid.uuid4())  # type: ignore[call-overload]


def test_get_scraper_invalid_type_raises():
    with pytest.raises(TypeError, match="Expected Store or UUID"):
        get_scraper("not-a-store")  # type: ignore[arg-type]


def test_create_scraper_coles():
    uid = uuid.uuid4()
    s = _create_scraper(Store.COLES, uid)
    assert isinstance(s, ColesScraper)
    assert s.user_id == uid


def test_create_scraper_woolworths():
    uid = uuid.uuid4()
    s = _create_scraper(Store.WOOLWORTHS, uid)
    assert isinstance(s, WoolworthsScraper)


def test_create_scraper_unknown_store_raises():
    with pytest.raises(KeyError):
        _create_scraper("unknown_store", None)  # type: ignore[arg-type]
