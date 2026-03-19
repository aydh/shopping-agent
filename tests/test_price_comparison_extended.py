from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from shopping_agent.models import Product, ProductMatch, Store
from shopping_agent.scrapers.base import ScrapedProduct
from shopping_agent.services.price_comparison import (
    _upsert_scraped_product,
    build_price_map,
    compare_product_prices,
    find_or_create_match,
    match_unmatched_products,
    matches_to_comparisons,
)


def _product(product_id: int, store: Store, name: str, price: float | None, unit_size: str | None = None) -> Product:
    return Product(
        id=product_id,
        store=store,
        store_product_id=f"{store.value}-{product_id}",
        name=name,
        current_price=price,
        unit_size=unit_size,
        is_available=True,
    )


@pytest.mark.asyncio
async def test_find_or_create_match_returns_existing_active_match(fake_result):
    source = _product(1, Store.COLES, "Milk", 4.0, "2L")
    existing = ProductMatch(product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[existing]))

    match = await find_or_create_match(session, source, Store.WOOLWORTHS)

    assert match is existing


@pytest.mark.asyncio
async def test_find_or_create_match_uses_local_fuzzy_match(fake_result, monkeypatch):
    source = _product(1, Store.COLES, "Full Cream Milk", 4.0, "2L")
    candidate = _product(2, Store.WOOLWORTHS, "Full Cream Milk", 4.5, "2L")
    inserted = ProductMatch(product_a_id=1, product_b_id=2, confidence=0.95, match_method="fuzzy_name")
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[]),
            fake_result(scalars=[candidate]),
        ]
    )
    insert_match = AsyncMock(return_value=inserted)
    monkeypatch.setattr("shopping_agent.services.price_comparison._insert_match_or_fetch_existing", insert_match)

    match = await find_or_create_match(session, source, Store.WOOLWORTHS)

    assert match is inserted
    insert_match.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_or_create_match_falls_back_to_scraper_search(fake_result, monkeypatch):
    source = _product(1, Store.COLES, "Special Milk", 4.0, "2L")
    scraped = ScrapedProduct(store_product_id="w-9", name="Special Milk", current_price=4.6, unit_size="2L")
    target = _product(9, Store.WOOLWORTHS, "Special Milk", 4.6, "2L")
    created = ProductMatch(product_a_id=1, product_b_id=9, confidence=0.88, match_method="search")
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[]),
            fake_result(scalars=[]),
        ]
    )
    scraper = AsyncMock()
    scraper.search_product = AsyncMock(return_value=[scraped])
    monkeypatch.setattr(
        "shopping_agent.services.price_comparison._upsert_scraped_product",
        AsyncMock(return_value=target),
    )
    insert_match = AsyncMock(return_value=created)
    monkeypatch.setattr("shopping_agent.services.price_comparison._insert_match_or_fetch_existing", insert_match)

    match = await find_or_create_match(session, source, Store.WOOLWORTHS, scraper=scraper)

    assert match is created
    scraper.search_product.assert_awaited_once()
    insert_match.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_scraped_product_updates_existing_product(fake_result):
    existing = _product(2, Store.WOOLWORTHS, "Milk", 4.0, "2L")
    scraped = ScrapedProduct(
        store_product_id=existing.store_product_id,
        name="Milk",
        current_price=4.5,
        is_available=False,
        unit_price=2.25,
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[existing]))

    product = await _upsert_scraped_product(session, scraped, Store.WOOLWORTHS)

    assert product is existing
    assert existing.current_price == 4.5
    assert existing.is_available is False
    assert existing.unit_price == 2.25


@pytest.mark.asyncio
async def test_upsert_scraped_product_inserts_new_product(fake_result):
    scraped = ScrapedProduct(
        store_product_id="w-3",
        name="Bread",
        current_price=3.2,
        brand="Brand",
        category="Bakery",
        unit_size="700g",
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[]))
    session.add = MagicMock()

    product = await _upsert_scraped_product(session, scraped, Store.WOOLWORTHS)

    assert product.store == Store.WOOLWORTHS
    assert product.store_product_id == "w-3"
    session.add.assert_called_once_with(product)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_match_unmatched_products_creates_pairs_and_skips_rejected(fake_result, async_cm):
    source = _product(1, Store.COLES, "Milk", 4.0, "2L")
    eligible = _product(2, Store.WOOLWORTHS, "Milk", 4.2, "2L")
    rejected = _product(3, Store.WOOLWORTHS, "Milk", 4.1, "2L")
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(rows=[]),
            fake_result(scalars=[source]),
            fake_result(scalars=[eligible, rejected]),
            fake_result(rows=[(1, 3)]),
        ]
    )
    session.add = MagicMock()
    session.begin_nested = MagicMock(return_value=async_cm(None))

    count = await match_unmatched_products(session, Store.COLES)

    assert count == 1
    added_match = session.add.call_args.args[0]
    assert isinstance(added_match, ProductMatch)
    assert {added_match.product_a_id, added_match.product_b_id} == {1, 2}
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_compare_product_prices_returns_partner_prices(fake_result, monkeypatch):
    coles_product = _product(1, Store.COLES, "Milk", 4.0, "2L")
    ww_product = _product(2, Store.WOOLWORTHS, "Milk", 5.0, "2L")
    match = ProductMatch(
        id=10,
        product_a_id=1,
        product_b_id=2,
        confidence=0.9,
        match_method="manual",
        is_confirmed=True,
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[coles_product]),
            fake_result(scalars=[ww_product]),
        ]
    )
    monkeypatch.setattr(
        "shopping_agent.services.price_comparison.find_or_create_match",
        AsyncMock(return_value=match),
    )

    comparisons = await compare_product_prices(session, [1])

    assert len(comparisons) == 1
    comp = comparisons[0]
    assert comp.coles_price == 4.0
    assert comp.woolworths_price == 5.0
    assert comp.cheaper_store == Store.COLES
    assert comp.savings == pytest.approx(1.0)
    assert comp.match_id == 10


def test_matches_to_comparisons_and_build_price_map_share_pair_data():
    coles_product = _product(1, Store.COLES, "Milk", 4.0, "2L")
    ww_product = _product(2, Store.WOOLWORTHS, "Milk", 4.5, "2L")
    match = ProductMatch(
        id=7,
        product_a_id=1,
        product_b_id=2,
        confidence=0.91,
        match_method="manual",
        is_confirmed=True,
    )
    match.product_a = coles_product
    match.product_b = ww_product

    comparisons = matches_to_comparisons([match])
    price_map = build_price_map([match])

    assert comparisons[0].cheaper_store == Store.COLES
    assert comparisons[0].match_method == "manual"
    assert price_map[1] == {"coles_price": 4.0, "woolworths_price": 4.5}
    assert price_map[2] == price_map[1]
