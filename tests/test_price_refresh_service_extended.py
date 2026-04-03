from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shopping_agent.models import Product, ShoppingListItem, Store
from shopping_agent.scrapers.base import ScrapedProduct
from shopping_agent.services.price_refresh import do_price_refresh


def _product(product_id: int, store: Store, name: str, price: float | None = None) -> Product:
    return Product(
        id=product_id,
        store=store,
        store_product_id=f"{store.value}-{product_id}",
        name=name,
        current_price=price,
        is_available=True,
    )


@pytest.mark.asyncio
async def test_do_price_refresh_updates_prices_and_list_items(fake_result, async_cm, monkeypatch):
    product = _product(1, Store.COLES, "Milk", 4.0)
    db_product = _product(1, Store.COLES, "Milk", 4.0)
    list_item = ShoppingListItem(id=5, shopping_list_id=1, product_id=1, quantity=1, coles_price=4.0)
    outer_session = AsyncMock()
    outer_session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[product]),
            fake_result(scalars=[]),
            fake_result(scalars=[]),
            fake_result(scalars=[list_item]),
        ]
    )

    inner_session = AsyncMock()
    inner_session.get = AsyncMock(side_effect=lambda model, obj_id: {Product: db_product, ShoppingListItem: list_item}.get(model))
    inner_session.execute = AsyncMock(return_value=fake_result(scalars=[]))
    inner_session.add = MagicMock()
    sessions = iter([outer_session, inner_session])
    monkeypatch.setattr(
        "shopping_agent.services.price_refresh.async_session",
        MagicMock(side_effect=lambda: async_cm(next(sessions))),
    )
    monkeypatch.setattr(
        "shopping_agent.services.price_refresh._coles_scraper",
        SimpleNamespace(
            get_product_price=AsyncMock(return_value=ScrapedProduct(
                store_product_id=product.store_product_id,
                name=product.name,
                current_price=4.5,
                unit_price=2.25,
                unit_price_measure="L",
                image_url="https://img",
                is_available=True,
            ))
        ),
    )

    updated, total = await do_price_refresh(Store.COLES)

    assert (updated, total) == (1, 1)
    assert db_product.current_price == 4.5
    assert db_product.unit_price == 2.25
    assert list_item.coles_price == 4.5
    inner_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_price_refresh_reports_progress(fake_result, async_cm, monkeypatch):
    product = _product(1, Store.COLES, "Milk", 4.0)
    db_product = _product(1, Store.COLES, "Milk", 4.0)
    list_item = ShoppingListItem(id=5, shopping_list_id=1, product_id=1, quantity=1, coles_price=4.0)
    outer_session = AsyncMock()
    outer_session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[product]),
            fake_result(scalars=[]),
            fake_result(scalars=[]),
            fake_result(scalars=[list_item]),
        ]
    )

    inner_session = AsyncMock()
    inner_session.get = AsyncMock(side_effect=lambda model, obj_id: {Product: db_product, ShoppingListItem: list_item}.get(model))
    inner_session.execute = AsyncMock(return_value=fake_result(scalars=[]))
    inner_session.add = MagicMock()
    sessions = iter([outer_session, inner_session])
    monkeypatch.setattr(
        "shopping_agent.services.price_refresh.async_session",
        MagicMock(side_effect=lambda: async_cm(next(sessions))),
    )
    monkeypatch.setattr(
        "shopping_agent.services.price_refresh._coles_scraper",
        SimpleNamespace(
            get_product_price=AsyncMock(return_value=ScrapedProduct(
                store_product_id=product.store_product_id,
                name=product.name,
                current_price=4.5,
                is_available=True,
            ))
        ),
    )
    progress_events: list[tuple[int, int]] = []

    async def on_progress(done: int, total: int) -> None:
        progress_events.append((done, total))

    updated, total = await do_price_refresh(Store.COLES, progress_callback=on_progress)

    assert (updated, total) == (1, 1)
    assert progress_events == [(0, 1), (1, 1)]


@pytest.mark.asyncio
async def test_do_price_refresh_marks_unavailable_products(fake_result, async_cm, monkeypatch):
    product = _product(2, Store.COLES, "Bread", 3.0)
    db_product = _product(2, Store.COLES, "Bread", 3.0)
    list_item = ShoppingListItem(id=6, shopping_list_id=1, product_id=2, quantity=1, coles_price=3.0)
    outer_session = AsyncMock()
    outer_session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[product]),
            fake_result(scalars=[]),
            fake_result(scalars=[]),
            fake_result(scalars=[list_item]),
        ]
    )

    inner_session = AsyncMock()
    inner_session.get = AsyncMock(side_effect=lambda model, obj_id: {Product: db_product, ShoppingListItem: list_item}.get(model))
    sessions = iter([outer_session, inner_session])
    monkeypatch.setattr(
        "shopping_agent.services.price_refresh.async_session",
        MagicMock(side_effect=lambda: async_cm(next(sessions))),
    )
    monkeypatch.setattr(
        "shopping_agent.services.price_refresh._coles_scraper",
        SimpleNamespace(
            get_product_price=AsyncMock(return_value=ScrapedProduct(
                store_product_id=product.store_product_id,
                name=product.name,
                current_price=0.0,
                is_available=False,
            ))
        ),
    )

    updated, total = await do_price_refresh(Store.COLES)

    assert (updated, total) == (0, 1)
    assert db_product.is_available is False
    assert db_product.current_price is None
    assert list_item.coles_price is None
    inner_session.commit.assert_awaited_once()
