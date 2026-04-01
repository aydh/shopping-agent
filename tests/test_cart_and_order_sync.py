from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from shopping_agent.models import ListStatus, Order, OrderItem, PriceHistory, Product, ShoppingList, ShoppingListItem, Store
from shopping_agent.scrapers.base import ScrapedOrder, ScrapedOrderItem
from shopping_agent.services.cart import _resolve_store_product_id, add_to_cart
from shopping_agent.services.order_sync import sync_orders

_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _product(product_id: int, store: Store, store_product_id: str, name: str, price: float | None = None) -> Product:
    return Product(
        id=product_id,
        store=store,
        store_product_id=store_product_id,
        name=name,
        current_price=price,
        is_available=True,
    )


@pytest.mark.asyncio
async def test_resolve_store_product_id_returns_own_store_id():
    session = AsyncMock()
    product = _product(1, Store.COLES, "abc", "Milk")

    resolved = await _resolve_store_product_id(session, product, Store.COLES)

    assert resolved == "abc"


@pytest.mark.asyncio
async def test_resolve_store_product_id_uses_partner_product(monkeypatch):
    product = _product(1, Store.COLES, "abc", "Milk")
    partner = _product(2, Store.WOOLWORTHS, "ww-1", "Milk")
    session = AsyncMock()
    monkeypatch.setattr(
        "shopping_agent.services.cart.get_partner_product",
        AsyncMock(return_value=partner),
    )

    resolved = await _resolve_store_product_id(session, product, Store.WOOLWORTHS)

    assert resolved == "ww-1"


@pytest.mark.asyncio
async def test_add_to_cart_returns_error_when_no_confirmed_list(fake_result):
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[]))

    mock_scraper = AsyncMock()
    result = await add_to_cart(session, Store.COLES, mock_scraper, mock_scraper)

    assert result == {"success": False, "error": "No confirmed shopping list found"}


@pytest.mark.asyncio
async def test_add_to_cart_reports_skipped_items_when_no_matches(fake_result, monkeypatch):
    product = _product(1, Store.COLES, "abc", "Milk")
    item = ShoppingListItem(id=10, product=product, product_id=1, quantity=2, chosen_store=Store.COLES)
    shopping_list = ShoppingList(
        id=1,
        name="List",
        target_date=date(2025, 3, 1),
        status=ListStatus.CONFIRMED,
        items=[item],
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[shopping_list]))
    monkeypatch.setattr("shopping_agent.services.cart._resolve_store_product_id", AsyncMock(return_value=None))
    mock_scraper = AsyncMock()

    result = await add_to_cart(session, Store.COLES, mock_scraper, mock_scraper)

    assert result["success"] is True
    assert result["count"] == 0
    assert "no coles product match" in result["message"].lower()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_add_to_cart_marks_successes_and_collects_failures(fake_result, monkeypatch):
    success_product = _product(1, Store.COLES, "c-1", "Milk")
    fail_product = _product(2, Store.COLES, "c-2", "Bread")
    success_item = ShoppingListItem(id=101, product=success_product, product_id=1, quantity=1, chosen_store=Store.COLES)
    fail_item = ShoppingListItem(id=102, product=fail_product, product_id=2, quantity=3, chosen_store=Store.COLES)
    shopping_list = ShoppingList(
        id=1,
        name="List",
        target_date=date(2025, 3, 1),
        status=ListStatus.CONFIRMED,
        items=[success_item, fail_item],
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[shopping_list]))
    session.get = AsyncMock(side_effect=lambda model, item_id: {101: success_item, 102: fail_item}.get(item_id))
    monkeypatch.setattr(
        "shopping_agent.services.cart._resolve_store_product_id",
        AsyncMock(side_effect=["c-1", "c-2"]),
    )
    scraper = MagicMock()
    scraper.add_to_cart = AsyncMock(return_value={"c-1": True, "c-2": False})
    scraper.get_cart_url = AsyncMock(return_value="https://example.test/cart")
    result = await add_to_cart(session, Store.COLES, scraper, AsyncMock())

    assert result == {
        "success": False,
        "count": 1,
        "cart_url": "https://example.test/cart",
        "message": "Added 1/2 items to coles cart",
        "failed_item_ids": [102],
    }
    assert success_item.is_ordered is True
    assert not fail_item.is_ordered
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_orders_returns_zero_for_empty_input():
    session = AsyncMock()

    count = await sync_orders(session, [], Store.COLES, _USER_ID)

    assert count == 0
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_sync_orders_updates_existing_order_metadata(fake_result):
    existing_order = Order(
        id=1,
        store=Store.COLES,
        store_order_id="ord-1",
        order_date=date(2025, 1, 1),
        total_amount=10.0,
        status="done",
    )
    scraped = ScrapedOrder(
        store_order_id="ord-1",
        order_date=date(2025, 1, 1),
        store_name="Coles Local",
        store_id="store-123",
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[existing_order]),
            fake_result(scalars=[]),
        ]
    )

    count = await sync_orders(session, [scraped], Store.COLES, _USER_ID)

    assert count == 0
    assert existing_order.store_name == "Coles Local"
    assert existing_order.store_id == "store-123"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_orders_inserts_orders_products_items_and_price_history(fake_result):
    scraped = ScrapedOrder(
        store_order_id="ord-2",
        order_date=date(2025, 2, 2),
        total_amount=12.5,
        status="delivered",
        items=[
            ScrapedOrderItem(
                store_product_id="c-123",
                name="Milk",
                quantity=2,
                price_paid=4.5,
                brand="Coles",
                unit_size="2L",
                image_url="https://img",
                category="Dairy",
            )
        ],
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[]),
            fake_result(scalars=[]),
            fake_result(scalars=[]),
        ]
    )
    added: list[object] = []

    def add(obj):
        added.append(obj)

    async def flush():
        next_product_id = 100
        next_order_id = 200
        for obj in added:
            if isinstance(obj, Product) and obj.id is None:
                obj.id = next_product_id
                next_product_id += 1
            if isinstance(obj, Order) and obj.id is None:
                obj.id = next_order_id
                next_order_id += 1

    session.add = MagicMock(side_effect=add)
    session.flush.side_effect = flush

    count = await sync_orders(session, [scraped], Store.COLES, _USER_ID)

    assert count == 1
    assert any(isinstance(obj, Order) and obj.store_order_id == "ord-2" for obj in added)
    assert any(isinstance(obj, Product) and obj.store_product_id == "c-123" for obj in added)
    assert any(isinstance(obj, OrderItem) and obj.quantity == 2 for obj in added)
    assert any(
        isinstance(obj, PriceHistory)
        and obj.price == 4.5
        and obj.recorded_at == datetime(2025, 2, 2, tzinfo=timezone.utc)
        for obj in added
    )
    session.commit.assert_awaited_once()
