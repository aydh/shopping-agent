"""Shared pytest fixtures for the shopping-agent test suite."""
import pytest
from datetime import date

from shopping_agent.services.prediction import PurchaseRecord


@pytest.fixture
def sample_purchases() -> list[PurchaseRecord]:
    """Three purchases at regular weekly intervals, 2 units each."""
    return [
        PurchaseRecord(order_date=date(2025, 1, 1), quantity=2),
        PurchaseRecord(order_date=date(2025, 1, 8), quantity=2),
        PurchaseRecord(order_date=date(2025, 1, 15), quantity=2),
    ]


@pytest.fixture
def irregular_purchases() -> list[PurchaseRecord]:
    """Purchases at irregular intervals to test confidence scoring."""
    return [
        PurchaseRecord(order_date=date(2025, 1, 1), quantity=1),
        PurchaseRecord(order_date=date(2025, 1, 20), quantity=3),
        PurchaseRecord(order_date=date(2025, 2, 5), quantity=1),
        PurchaseRecord(order_date=date(2025, 3, 1), quantity=2),
    ]
