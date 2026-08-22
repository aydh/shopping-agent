"""Tests for scrapers/base.py: default method stubs, stream_order_history, _load_cookies."""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shopping_agent.models.product import Store
from shopping_agent.scrapers.base import BaseScraper, ScrapedOrder, ScrapedOrderItem


# ---------------------------------------------------------------------------
# Minimal concrete subclass — implements the abstract methods
# ---------------------------------------------------------------------------

class _StubScraper(BaseScraper):
    store = Store.COLES
    _cookie_domain = "coles.com.au"

    async def is_authenticated(self) -> bool:
        return False

    async def login_interactive(self) -> bool:
        return False

    async def search_product(self, query: str):
        return []

    async def get_product_price(self, store_product_id, product_name=None, timeout=None):
        return None

    async def add_to_cart(self, items):
        return {}

    async def get_cart_url(self):
        return "https://coles.com.au/cart"

    async def get_order_history(self, limit=10):
        from datetime import date
        return [
            ScrapedOrder(
                store_order_id="o1",
                order_date=date(2025, 1, 1),
                total_amount=42.0,
                items=[ScrapedOrderItem(store_product_id="123", name="Milk", quantity=2, price_paid=2.5)],
            )
        ]


# ---------------------------------------------------------------------------
# Default stub method implementations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_cookies_default_returns_false():
    s = _StubScraper()
    assert await s.import_cookies("[]") is False


@pytest.mark.asyncio
async def test_logout_default_returns_none():
    s = _StubScraper()
    assert await s.logout() is None


@pytest.mark.asyncio
async def test_validate_cookies_default_returns_not_implemented():
    s = _StubScraper()
    result = await s.validate_cookies()
    assert result["ok"] is False
    assert "Not implemented" in result["detail"]


@pytest.mark.asyncio
async def test_login_with_credentials_default_returns_failed():
    s = _StubScraper()
    result = await s.login_with_credentials("a@b.com", "pw")
    assert result.startswith("failed:")


@pytest.mark.asyncio
async def test_complete_mfa_default_returns_failed():
    s = _StubScraper()
    result = await s.complete_mfa("123456")
    assert result.startswith("failed:")


@pytest.mark.asyncio
async def test_cancel_pending_login_default_returns_none():
    s = _StubScraper()
    assert await s.cancel_pending_login() is None


# ---------------------------------------------------------------------------
# stream_order_history — default wraps get_order_history
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_order_history_yields_all_orders():
    s = _StubScraper()
    orders = [o async for o in s.stream_order_history()]
    assert len(orders) == 1
    assert orders[0].store_order_id == "o1"


# ---------------------------------------------------------------------------
# _load_cookies
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_cookies_no_row_returns_empty_jar(monkeypatch, async_cm):
    from shopping_agent.scrapers import base as base_mod

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    factory = MagicMock(return_value=async_cm(session))
    monkeypatch.setattr(base_mod, "async_session", factory, raising=False)

    # Patch at the import level used inside _load_cookies
    with patch("shopping_agent.database.async_session", factory):
        s = _StubScraper()
        s.user_id = uuid.uuid4()
        jar = await s._load_cookies()
    assert isinstance(jar, httpx.Cookies)
    assert list(jar.jar) == []


@pytest.mark.asyncio
async def test_load_cookies_with_row_parses_json(monkeypatch, async_cm):
    cookies_data = [{"name": "session", "value": "abc", "domain": "coles.com.au", "path": "/"}]
    row = MagicMock()
    row.cookies_json = json.dumps(cookies_data)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=row)))
    factory = MagicMock(return_value=async_cm(session))

    with patch("shopping_agent.database.async_session", factory):
        s = _StubScraper()
        s.user_id = uuid.uuid4()
        jar = await s._load_cookies()
    assert jar.get("session", domain="coles.com.au") == "abc"


@pytest.mark.asyncio
async def test_load_cookies_empty_json_logs_warning(monkeypatch, async_cm, caplog):
    row = MagicMock()
    row.cookies_json = ""

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=row)))
    factory = MagicMock(return_value=async_cm(session))

    with patch("shopping_agent.database.async_session", factory):
        s = _StubScraper()
        s.user_id = uuid.uuid4()
        with caplog.at_level("WARNING"):
            jar = await s._load_cookies()
    assert isinstance(jar, httpx.Cookies)


@pytest.mark.asyncio
async def test_load_cookies_invalid_json_logs_error(monkeypatch, async_cm, caplog):
    row = MagicMock()
    row.cookies_json = "not-json"

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=row)))
    factory = MagicMock(return_value=async_cm(session))

    with patch("shopping_agent.database.async_session", factory):
        s = _StubScraper()
        s.user_id = uuid.uuid4()
        with caplog.at_level("ERROR"):
            jar = await s._load_cookies()
    assert isinstance(jar, httpx.Cookies)


@pytest.mark.asyncio
async def test_load_cookies_invalid_structure_logs_error(monkeypatch, async_cm, caplog):
    # Missing required 'name' field
    row = MagicMock()
    row.cookies_json = json.dumps([{"value": "abc"}])

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=row)))
    factory = MagicMock(return_value=async_cm(session))

    with patch("shopping_agent.database.async_session", factory):
        s = _StubScraper()
        s.user_id = uuid.uuid4()
        with caplog.at_level("ERROR"):
            jar = await s._load_cookies()
    assert isinstance(jar, httpx.Cookies)


# ---------------------------------------------------------------------------
# _save_cookies_from_client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_cookies_from_client_no_client_is_noop():
    s = _StubScraper()
    # No _client attribute — should silently return
    await s._save_cookies_from_client()


@pytest.mark.asyncio
async def test_save_cookies_from_client_inserts_when_no_row(monkeypatch, async_cm):
    import httpx

    cookie_jar = httpx.Cookies()
    cookie_jar.set("tok", "xyz", domain="coles.com.au")

    client = MagicMock()
    client.cookies = cookie_jar

    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    factory = MagicMock(return_value=async_cm(session))

    with patch("shopping_agent.database.async_session", factory):
        s = _StubScraper()
        s.user_id = uuid.uuid4()
        s._client = client  # type: ignore[attr-defined]
        await s._save_cookies_from_client()

    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_cookies_from_client_updates_existing_row(monkeypatch, async_cm):
    import httpx

    cookie_jar = httpx.Cookies()
    cookie_jar.set("tok", "new", domain="coles.com.au")

    client = MagicMock()
    client.cookies = cookie_jar

    existing_row = MagicMock()
    existing_row.cookies_json = "[]"
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_row))
    )
    factory = MagicMock(return_value=async_cm(session))

    with patch("shopping_agent.database.async_session", factory):
        s = _StubScraper()
        s.user_id = uuid.uuid4()
        s._client = client  # type: ignore[attr-defined]
        await s._save_cookies_from_client()

    assert json.loads(existing_row.cookies_json)[0]["name"] == "tok"
    session.commit.assert_awaited_once()
