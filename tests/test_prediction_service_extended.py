from __future__ import annotations

import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from shopping_agent.models import ConsumptionPrediction, Order, OrderItem, Product, ProductMatch, Store
from shopping_agent.services.prediction import get_predictions_with_match_info, refresh_predictions

_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _prediction(product_id: int, runout: date) -> ConsumptionPrediction:
    return ConsumptionPrediction(
        product_id=product_id,
        avg_purchase_interval_days=7.0,
        avg_quantity_per_purchase=2.0,
        estimated_daily_consumption=0.25,
        confidence_score=0.8,
        last_purchased_date=runout - timedelta(days=5),
        predicted_runout_date=runout,
        next_purchase_date=runout - timedelta(days=2),
        purchase_count=3,
        last_purchase_quantity=2,
        last_purchase_store="coles",
    )


def _ordered_product(
    product_id: int,
    store: Store,
    store_product_id: str,
    order_dates: list[date],
    quantity: int = 1,
) -> Product:
    product = Product(id=product_id, store=store, store_product_id=store_product_id, name=f"Product {product_id}")
    items = []
    for index, order_date in enumerate(order_dates, start=1):
        order = Order(
            id=index,
            store=store,
            store_order_id=f"{store.value}-{index}",
            order_date=order_date,
            total_amount=10.0,
        )
        item = OrderItem(id=index, order=order, product=product, quantity=quantity, price_paid=3.5)
        items.append(item)
    product.order_items = items
    return product


@pytest.mark.asyncio
async def test_refresh_predictions_updates_canonical_prediction_and_deletes_member_prediction(fake_result):
    today = date.today()
    product_a = _ordered_product(1, Store.COLES, "c-1", [today - timedelta(days=20), today - timedelta(days=13)])
    product_b = _ordered_product(2, Store.WOOLWORTHS, "w-1", [today - timedelta(days=6)])
    canonical = _prediction(1, today + timedelta(days=1))
    stale_member = _prediction(2, today + timedelta(days=1))
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[product_a, product_b]),
            fake_result(scalars=[ProductMatch(product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual")]),
            fake_result(rows=[]),  # excluded_product_ids — none excluded
            fake_result(scalars=[canonical, stale_member]),
        ]
    )

    count = await refresh_predictions(session, _USER_ID)

    assert count == 1
    assert canonical.product_id == 1
    assert canonical.purchase_count == 3
    session.delete.assert_awaited_once_with(stale_member)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_predictions_deletes_old_predictions_for_stale_products(fake_result):
    very_old = date.today() - timedelta(days=500)
    product = _ordered_product(3, Store.COLES, "c-3", [very_old, very_old + timedelta(days=7)])
    existing = _prediction(3, date.today())
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[product]),
            fake_result(scalars=[]),
            fake_result(rows=[]),  # excluded_product_ids — none excluded
            fake_result(scalars=[existing]),
        ]
    )

    count = await refresh_predictions(session, _USER_ID)

    assert count == 0
    session.delete.assert_awaited_once_with(existing)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_predictions_skips_excluded_products(fake_result):
    today = date.today()
    product = _ordered_product(1, Store.COLES, "c-1", [today - timedelta(days=20), today - timedelta(days=13)])
    existing = _prediction(1, today + timedelta(days=5))
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[product]),
            fake_result(scalars=[]),       # no matches
            fake_result(rows=[(1,)]),      # product 1 is excluded from predictions
            fake_result(scalars=[existing]),
        ]
    )

    count = await refresh_predictions(session, _USER_ID)

    assert count == 0
    session.delete.assert_awaited_once_with(existing)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_predictions_with_match_info_includes_partner_metadata(fake_result):
    runout = date.today() + timedelta(days=3)
    product = Product(id=1, store=Store.COLES, store_product_id="c-1", name="Milk")
    partner = Product(id=2, store=Store.WOOLWORTHS, store_product_id="w-1", name="Milk")
    prediction = _prediction(1, runout)
    prediction.product = product
    match = ProductMatch(
        id=9,
        product_a_id=1,
        product_b_id=2,
        confidence=0.92,
        match_method="manual",
        is_confirmed=True,
    )
    match.product_a = product
    match.product_b = partner
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[prediction]),
            fake_result(scalars=[match]),
        ]
    )

    views = await get_predictions_with_match_info(session, _USER_ID, max_runout_date=runout)

    assert len(views) == 1
    view = views[0]
    assert view.product_id == 1
    assert view.days_until_runout == 3
    assert view.is_matched is True
    assert view.matched_product is partner
    assert view.match_id == 9
