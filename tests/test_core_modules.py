from __future__ import annotations

from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shopping_agent.cache import InMemoryImageCache
from shopping_agent.config import Settings
from shopping_agent.services.data_management import get_db_counts
from shopping_agent.templating import _localtime, _product_image_url, _product_url


@pytest.mark.asyncio
async def test_in_memory_image_cache_returns_none_for_missing():
    cache = InMemoryImageCache()

    assert await cache.get("missing") is None


@pytest.mark.asyncio
async def test_in_memory_image_cache_expires_entries(monkeypatch):
    cache = InMemoryImageCache(default_ttl=5)
    current = {"value": 100.0}
    monkeypatch.setattr("shopping_agent.cache.time.monotonic", lambda: current["value"])

    await cache.set("img", b"bytes", "image/png")
    current["value"] = 104.0
    assert await cache.get("img") == (b"bytes", "image/png")
    current["value"] = 106.0
    assert await cache.get("img") is None


@pytest.mark.asyncio
async def test_in_memory_image_cache_evicts_oldest_entries(monkeypatch):
    cache = InMemoryImageCache(max_entries=10)
    current = {"value": 100.0}
    monkeypatch.setattr("shopping_agent.cache.time.monotonic", lambda: current["value"])

    for index in range(10):
        current["value"] += 1.0
        await cache.set(f"k{index}", f"v{index}".encode(), "image/jpeg")

    current["value"] += 1.0
    await cache.set("k10", b"v10", "image/jpeg")

    assert await cache.get("k0") is None
    assert await cache.get("k10") == (b"v10", "image/jpeg")


def test_product_image_url_uses_proxy_for_coles_images():
    proxied = _product_image_url("https://productimages.coles.com.au/foo bar.jpg")

    assert proxied == "/api/prices/image-proxy?url=https%3A%2F%2Fproductimages.coles.com.au%2Ffoo%20bar.jpg"


def test_product_image_url_returns_original_for_other_hosts():
    url = "https://cdn.woolworths.com.au/img.jpg"

    assert _product_image_url(url) == url


@pytest.mark.parametrize(
    ("url", "store_product_id", "store", "name", "expected"),
    [
        ("https://example.com/product", None, None, None, "https://example.com/product"),
        ("legacy-slug", "123", None, None, "https://www.woolworths.com.au/shop/productdetails/123/legacy-slug"),
        (None, "456", "coles", "Full Cream Milk", "https://www.coles.com.au/product/full-cream-milk-456"),
        (None, "789", "woolworths", None, "https://www.woolworths.com.au/shop/productdetails/789"),
        (None, None, "coles", "Milk", None),
    ],
)
def test_product_url_variants(url, store_product_id, store, name, expected):
    assert _product_url(url, store_product_id=store_product_id, store=store, name=name) == expected


def test_settings_ensure_dirs_creates_data_and_log_directories(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://example/db",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )

    settings.ensure_dirs()

    assert settings.data_dir.is_dir()
    assert settings.log_dir.is_dir()


@pytest.mark.asyncio
async def test_get_db_counts_aggregates_all_tables(fake_result):
    from shopping_agent.models import Store
    row = namedtuple("Row", ["store", "count"])

    results = [
        fake_result(rows=[row(Store.COLES, 3), row(Store.WOOLWORTHS, 2)]),
        fake_result(rows=[row(Store.COLES, 11), row(Store.WOOLWORTHS, 12)]),
        fake_result(rows=[row(Store.COLES, 21), row(Store.WOOLWORTHS, 22)]),
        fake_result(scalar=4),
        fake_result(scalar=5),
        fake_result(scalar=6),
        fake_result(scalar=7),
        fake_result(scalar=8),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)

    counts = await get_db_counts(session)

    assert counts == {
        "coles_orders": 3,
        "woolworths_orders": 2,
        "coles_order_items": 11,
        "woolworths_order_items": 12,
        "coles_products": 21,
        "woolworths_products": 22,
        "product_matches": 4,
        "price_history": 5,
        "predictions": 6,
        "shopping_lists": 7,
        "shopping_list_items": 8,
    }


@pytest.mark.asyncio
async def test_get_session_yields_async_session(monkeypatch, async_cm):
    from shopping_agent import database

    session = object()
    factory = MagicMock(return_value=async_cm(session))
    monkeypatch.setattr(database, "async_session", factory)

    yielded_sessions = []
    async for yielded in database.get_session():
        yielded_sessions.append(yielded)

    assert yielded_sessions == [session]
    factory.assert_called_once_with()


@pytest.mark.asyncio
async def test_init_db_executes_healthcheck(monkeypatch, async_cm):
    from shopping_agent import database

    conn = AsyncMock()
    engine = SimpleNamespace(connect=MagicMock(return_value=async_cm(conn)))
    monkeypatch.setattr(database, "engine", engine)

    await database.init_db()

    conn.execute.assert_awaited_once()
    statement = conn.execute.await_args.args[0]
    assert str(statement) == "SELECT 1"


@pytest.mark.asyncio
async def test_verify_db_connection_executes_minimal_query(monkeypatch, async_cm):
    from shopping_agent import database

    conn = AsyncMock()
    engine = SimpleNamespace(connect=MagicMock(return_value=async_cm(conn)))
    monkeypatch.setattr(database, "engine", engine)

    await database.verify_db_connection()

    conn.execute.assert_awaited_once()
    statement = conn.execute.await_args.args[0]
    assert str(statement) == "SELECT 1"


@pytest.mark.asyncio
async def test_app_lifespan_runs_init_db(monkeypatch):
    from shopping_agent import main

    init_db = AsyncMock()
    monkeypatch.setattr(main, "init_db", init_db)

    async with main.app_lifespan(main.app):
        pass

    init_db.assert_awaited_once()


def test_main_app_mounts_static_and_mcp():
    from shopping_agent.main import app

    paths = {route.path for route in app.routes}

    assert "/static" in paths
    assert "/mcp" in paths
    assert "/" in paths
    assert "/healthz" in paths


# ---------------------------------------------------------------------------
# templating._localtime
# ---------------------------------------------------------------------------

def test_localtime_naive_datetime_gets_utc_assumed():
    from datetime import datetime
    naive = datetime(2025, 6, 1, 10, 0, 0)
    result = _localtime(naive)
    # Should have timezone info after conversion
    assert result.tzinfo is not None


def test_localtime_aware_datetime_converts():
    from datetime import datetime, timezone
    aware = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = _localtime(aware)
    assert result.tzinfo is not None


def test_localtime_none_passthrough():
    assert _localtime(None) is None  # type: ignore[arg-type]


def test_product_image_url_none_returns_none():
    assert _product_image_url(None) is None


# ---------------------------------------------------------------------------
# templating._get_nav_user
# ---------------------------------------------------------------------------

def test_get_nav_user_no_cookie_returns_none(make_request):
    from shopping_agent.templating import _get_nav_user
    request = make_request("/")
    assert _get_nav_user(request) is None


def test_get_nav_user_bad_token_returns_none(make_request):
    from unittest.mock import patch
    from shopping_agent.templating import _get_nav_user

    request = make_request("/")
    request._cookies = {"sb-access-token": "bad.token"}  # type: ignore[attr-defined]
    with patch.object(type(request), "cookies", property(lambda self: self._cookies)):
        result = _get_nav_user(request)
    assert result is None
