from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from shopping_agent.models import ConsumptionPrediction, ListStatus, Product, ProductMatch, ShoppingList, ShoppingListItem, Store
from shopping_agent.services.prediction import ShoppingListCandidate
from shopping_agent.services.shopping_list import (
    add_item_to_list,
    confirm_list,
    generate_shopping_list,
    get_list_history,
    get_shopping_list_context,
    remove_item,
    resolve_display_names,
    update_item_quantity,
    update_item_store,
)


def _product(product_id: int, store: Store, name: str, price: float | None) -> Product:
    return Product(
        id=product_id,
        store=store,
        store_product_id=f"{store.value}-{product_id}",
        name=name,
        current_price=price,
        is_available=True,
    )


@pytest.mark.asyncio
async def test_generate_shopping_list_creates_new_draft_from_candidates(fake_result, monkeypatch):
    product = _product(1, Store.COLES, "Milk", 4.0)
    partner = _product(2, Store.WOOLWORTHS, "Milk", 4.5)
    prediction = ConsumptionPrediction(
        product_id=1,
        avg_purchase_interval_days=7.0,
        avg_quantity_per_purchase=2.0,
        estimated_daily_consumption=0.3,
        confidence_score=0.9,
        last_purchased_date=date(2025, 1, 15),
        predicted_runout_date=date(2025, 1, 22),
        next_purchase_date=date(2025, 1, 20),
        purchase_count=4,
        last_purchase_quantity=2,
        last_purchase_store="coles",
    )
    prediction.product = product
    match = ProductMatch(product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual")
    match.product_a = product
    match.product_b = partner
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[prediction]),
            fake_result(scalars=[match]),
            fake_result(scalars=[]),
        ]
    )
    monkeypatch.setattr(
        "shopping_agent.services.shopping_list.generate_candidates",
        lambda predictions, target_date, lookahead_days: [ShoppingListCandidate(product_id=1, quantity=3, reason="Predicted runout: 2025-01-22")],
    )
    added: list[object] = []
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))

    async def flush():
        for obj in added:
            if isinstance(obj, ShoppingList) and obj.id is None:
                obj.id = 50

    session.flush.side_effect = flush

    shopping_list = await generate_shopping_list(session, target_date=date(2025, 1, 20))

    assert shopping_list.id == 50
    items = [obj for obj in added if isinstance(obj, ShoppingListItem)]
    assert len(items) == 1
    assert items[0].coles_price == 4.0
    assert items[0].woolworths_price == 4.5
    assert items[0].chosen_store == Store.COLES
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_shopping_list_reuses_existing_draft_and_deletes_auto_items(fake_result, monkeypatch):
    product = _product(1, Store.COLES, "Milk", 4.0)
    prediction = ConsumptionPrediction(
        product_id=1,
        avg_purchase_interval_days=7.0,
        avg_quantity_per_purchase=2.0,
        estimated_daily_consumption=0.3,
        confidence_score=0.9,
        last_purchased_date=date(2025, 1, 15),
        predicted_runout_date=date(2025, 1, 22),
        next_purchase_date=date(2025, 1, 20),
        purchase_count=4,
        last_purchase_quantity=2,
        last_purchase_store="coles",
    )
    prediction.product = product
    existing_list = ShoppingList(id=10, name="Old", target_date=date(2025, 1, 1), status=ListStatus.DRAFT)
    auto_item = ShoppingListItem(id=5, shopping_list_id=10, product_id=1, quantity=1, is_user_added=False)
    manual_item = ShoppingListItem(id=6, shopping_list_id=10, product_id=1, quantity=1, is_user_added=True)
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[prediction]),
            fake_result(scalars=[]),
            fake_result(scalars=[existing_list]),
            fake_result(scalars=[auto_item, manual_item]),
        ]
    )
    monkeypatch.setattr(
        "shopping_agent.services.shopping_list.generate_candidates",
        lambda predictions, target_date, lookahead_days: [],
    )

    shopping_list = await generate_shopping_list(session, target_date=date(2025, 2, 1))

    assert shopping_list is existing_list
    assert existing_list.name == "Week of 2025-02-01"
    assert existing_list.target_date == date(2025, 2, 1)
    session.delete.assert_awaited_once_with(auto_item)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_quantity_store_remove_and_confirm_mutate_items():
    item = ShoppingListItem(id=1, shopping_list_id=1, product_id=1, quantity=2, chosen_store=Store.COLES)
    shopping_list = ShoppingList(id=99, name="List", target_date=date(2025, 1, 1), status=ListStatus.DRAFT)
    session = AsyncMock()
    session.get = AsyncMock(side_effect=lambda model, item_id: {1: item, 99: shopping_list}.get(item_id))

    updated = await update_item_quantity(session, 1, 5)
    removed = await remove_item(session, 1)
    stored = await update_item_store(session, 1, Store.WOOLWORTHS)
    confirmed = await confirm_list(session, 99)

    assert updated.quantity == 5
    assert removed is True
    assert item.is_removed is True
    assert stored.chosen_store == Store.WOOLWORTHS
    assert confirmed.status == ListStatus.CONFIRMED
    assert session.commit.await_count == 4


@pytest.mark.asyncio
async def test_add_item_to_list_increments_existing_item(fake_result, monkeypatch):
    active_list = ShoppingList(id=1, name="List", target_date=date(2025, 1, 1), status=ListStatus.DRAFT)
    product = _product(1, Store.COLES, "Milk", 4.0)
    existing = ShoppingListItem(id=7, shopping_list_id=1, product_id=1, quantity=2, chosen_store=Store.COLES)
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[active_list]),
            fake_result(scalars=[existing]),
        ]
    )
    session.get = AsyncMock(return_value=product)
    monkeypatch.setattr(
        "shopping_agent.services.product_resolution.get_partner_product",
        AsyncMock(return_value=None),
    )

    item = await add_item_to_list(session, product_id=1, quantity=3)

    assert item is existing
    assert existing.quantity == 5
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_item_to_list_uses_partner_prices_when_creating(fake_result, monkeypatch):
    active_list = ShoppingList(id=1, name="List", target_date=date(2025, 1, 1), status=ListStatus.DRAFT)
    product = _product(1, Store.COLES, "Milk", 5.0)
    partner = _product(2, Store.WOOLWORTHS, "Milk", 4.0)
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[active_list]),
            fake_result(scalars=[]),
        ]
    )
    session.get = AsyncMock(return_value=product)
    added: list[object] = []
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    monkeypatch.setattr(
        "shopping_agent.services.product_resolution.get_partner_product",
        AsyncMock(return_value=partner),
    )

    item = await add_item_to_list(session, product_id=1, quantity=1)

    assert item.product_id == 1
    assert item.coles_price == 5.0
    assert item.woolworths_price == 4.0
    assert item.chosen_store == Store.WOOLWORTHS
    assert item in added


@pytest.mark.asyncio
async def test_add_item_to_list_recovers_from_integrity_error(fake_result, monkeypatch):
    active_list = ShoppingList(id=1, name="List", target_date=date(2025, 1, 1), status=ListStatus.DRAFT)
    product = _product(1, Store.COLES, "Milk", 5.0)
    existing = ShoppingListItem(id=7, shopping_list_id=1, product_id=1, quantity=1, chosen_store=Store.COLES)
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[active_list]),
            fake_result(scalars=[]),
            fake_result(scalars=[existing]),
        ]
    )
    session.get = AsyncMock(return_value=product)
    session.add = MagicMock()
    session.commit = AsyncMock(side_effect=[IntegrityError("stmt", "params", "orig"), None])
    monkeypatch.setattr(
        "shopping_agent.services.product_resolution.get_partner_product",
        AsyncMock(return_value=None),
    )

    item = await add_item_to_list(session, product_id=1, quantity=2)

    assert item is existing
    assert existing.quantity == 3
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_display_names_prefers_chosen_store_partner(fake_result):
    coles_product = _product(1, Store.COLES, "Coles Milk", 4.0)
    ww_product = _product(2, Store.WOOLWORTHS, "WW Milk", 4.5)
    item = ShoppingListItem(id=9, shopping_list_id=1, product_id=1, quantity=1, chosen_store=Store.WOOLWORTHS)
    item.product = coles_product
    match = ProductMatch(product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual")
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[match]),
            fake_result(scalars=[ww_product]),
        ]
    )

    display_names, store_names, store_products = await resolve_display_names(session, [item])

    assert display_names[9] == "WW Milk"
    assert store_names[9] == {"coles": "Coles Milk", "woolworths": "WW Milk"}
    assert store_products[9]["woolworths"] is ww_product


@pytest.mark.asyncio
async def test_get_shopping_list_context_computes_totals_and_recommendation(fake_result, monkeypatch):
    coles_product = _product(1, Store.COLES, "Milk", 4.0)
    ww_product = _product(2, Store.WOOLWORTHS, "Milk", 5.0)
    item = ShoppingListItem(
        id=3,
        shopping_list_id=1,
        product_id=1,
        quantity=2,
        coles_price=4.0,
        woolworths_price=5.0,
        chosen_store=Store.COLES,
    )
    item.product = coles_product
    shopping_list = ShoppingList(
        id=1,
        name="List",
        target_date=date(2025, 1, 1),
        status=ListStatus.DRAFT,
        items=[item],
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[shopping_list]))
    monkeypatch.setattr(
        "shopping_agent.services.shopping_list.resolve_display_names",
        AsyncMock(return_value=({3: "Milk"}, {3: {"coles": "Milk", "woolworths": "Milk"}}, {3: {"coles": coles_product, "woolworths": ww_product}})),
    )

    ctx = await get_shopping_list_context(session)

    assert ctx["shopping_list"] is shopping_list
    assert ctx["single_store"] == Store.COLES
    assert ctx["coles_total"] == pytest.approx(8.0)
    assert ctx["store_metrics"]["coles"]["matched_available_count"] == 1
    assert ctx["store_metrics"]["coles"]["matched_available_total"] == pytest.approx(8.0)
    assert ctx["woolworths_total"] == pytest.approx(10.0)
    assert ctx["store_metrics"]["woolworths"]["available_count"] == 1
    assert ctx["best_total"] == pytest.approx(8.0)
    assert ctx["store_metrics"]["coles"]["available_count"] == 1
    assert ctx["store_metrics"]["coles"]["available_total"] == pytest.approx(8.0)
    assert ctx["store_metrics"]["woolworths"]["matched_available_count"] == ctx["store_metrics"]["coles"]["matched_available_count"]
    assert ctx["store_metrics"]["woolworths"]["matched_available_count"] == 1
    assert ctx["store_metrics"]["woolworths"]["matched_available_total"] == pytest.approx(10.0)
    assert "Coles is $2.00 cheaper overall" == ctx["recommendation"]


@pytest.mark.asyncio
async def test_get_shopping_list_context_builds_store_availability_metrics(fake_result, monkeypatch):
    coles_product = _product(1, Store.COLES, "Milk", 4.0)
    ww_product = _product(2, Store.WOOLWORTHS, "Milk", 5.0)
    ww_only_product = _product(3, Store.WOOLWORTHS, "Bread", 3.0)
    coles_partner = _product(4, Store.COLES, "Eggs", None)
    ww_source_product = _product(5, Store.WOOLWORTHS, "Eggs", 6.0)

    matched_item = ShoppingListItem(
        id=3,
        shopping_list_id=1,
        product_id=1,
        quantity=2,
        coles_price=4.0,
        woolworths_price=5.0,
        chosen_store=Store.COLES,
    )
    matched_item.product = coles_product

    unmatched_item = ShoppingListItem(
        id=4,
        shopping_list_id=1,
        product_id=3,
        quantity=1,
        coles_price=None,
        woolworths_price=3.0,
        chosen_store=Store.WOOLWORTHS,
    )
    unmatched_item.product = ww_only_product

    unavailable_item = ShoppingListItem(
        id=5,
        shopping_list_id=1,
        product_id=5,
        quantity=1,
        coles_price=None,
        woolworths_price=6.0,
        chosen_store=Store.WOOLWORTHS,
    )
    unavailable_item.product = ww_source_product

    shopping_list = ShoppingList(
        id=1,
        name="List",
        target_date=date(2025, 1, 1),
        status=ListStatus.DRAFT,
        items=[matched_item, unmatched_item, unavailable_item],
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[shopping_list]))
    monkeypatch.setattr(
        "shopping_agent.services.shopping_list.resolve_display_names",
        AsyncMock(
            return_value=(
                {3: "Milk", 4: "Bread", 5: "Eggs"},
                {
                    3: {"coles": "Milk", "woolworths": "Milk"},
                    4: {"coles": None, "woolworths": "Bread"},
                    5: {"coles": "Eggs", "woolworths": "Eggs"},
                },
                {
                    3: {"coles": coles_product, "woolworths": ww_product},
                    4: {"coles": None, "woolworths": ww_only_product},
                    5: {"coles": coles_partner, "woolworths": ww_source_product},
                },
            )
        ),
    )

    ctx = await get_shopping_list_context(session)

    assert ctx["store_metrics"]["coles"] == {
        "available_count": 1,
        "available_total": 8.0,
        "unavailable_count": 1,
        "unmatched_count": 1,
        "matched_available_count": 1,
        "matched_available_total": 8.0,
    }
    assert ctx["store_metrics"]["woolworths"] == {
        "available_count": 3,
        "available_total": 19.0,
        "unavailable_count": 0,
        "unmatched_count": 0,
        "matched_available_count": 1,
        "matched_available_total": 10.0,
    }


@pytest.mark.asyncio
async def test_get_list_history_summarizes_completed_lists(fake_result):
    item = ShoppingListItem(
        id=1,
        shopping_list_id=1,
        product_id=1,
        quantity=3,
        coles_price=2.0,
        chosen_store=Store.COLES,
    )
    shopping_list = ShoppingList(
        id=1,
        name="Past List",
        target_date=date(2025, 1, 1),
        status=ListStatus.ORDERED,
        created_at=datetime(2025, 1, 1, 12, 0, 0),
        items=[item],
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[shopping_list]))

    history = await get_list_history(session)

    assert history == [
        {
            "id": 1,
            "name": "Past List",
            "created_at": datetime(2025, 1, 1, 12, 0, 0),
            "status": ListStatus.ORDERED,
            "store": Store.COLES,
            "item_count": 1,
            "total": 6.0,
        }
    ]
