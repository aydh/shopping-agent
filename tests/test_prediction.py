"""Tests for shopping_agent.services.prediction."""
import math
from datetime import date

import pytest

from shopping_agent.services.prediction import (
    PurchaseRecord,
    ShoppingListCandidate,
    compute_prediction,
    generate_candidates,
)


class TestComputePrediction:
    def test_returns_none_with_single_purchase(self):
        purchases = [PurchaseRecord(order_date=date(2025, 1, 1), quantity=1)]
        assert compute_prediction(purchases) is None

    def test_returns_none_with_empty_list(self):
        assert compute_prediction([]) is None

    def test_regular_weekly_purchases_produce_prediction(self, sample_purchases):
        result = compute_prediction(sample_purchases, today=date(2025, 1, 15))
        assert result is not None
        assert result["avg_purchase_interval_days"] == pytest.approx(7.0, abs=0.5)
        assert result["avg_quantity_per_purchase"] == pytest.approx(2.0, abs=0.1)
        assert result["purchase_count"] == 3

    def test_runout_date_is_after_last_purchase(self, sample_purchases):
        result = compute_prediction(sample_purchases, today=date(2025, 1, 15))
        assert result is not None
        assert result["predicted_runout_date"] > date(2025, 1, 15)

    def test_next_purchase_date_before_runout(self, sample_purchases):
        result = compute_prediction(sample_purchases, today=date(2025, 1, 15))
        assert result is not None
        assert result["next_purchase_date"] < result["predicted_runout_date"]

    def test_confidence_between_zero_and_one(self, sample_purchases):
        result = compute_prediction(sample_purchases)
        assert result is not None
        assert 0.0 <= result["confidence_score"] <= 1.0

    def test_irregular_purchases_return_valid_confidence(self, irregular_purchases):
        result = compute_prediction(irregular_purchases)
        assert result is not None
        assert 0.0 <= result["confidence_score"] <= 1.0

    def test_daily_consumption_is_positive(self, sample_purchases):
        result = compute_prediction(sample_purchases)
        assert result is not None
        assert result["estimated_daily_consumption"] > 0

    def test_duplicate_dates_are_ignored(self):
        """Purchases on the same date produce a zero interval that is skipped.

        With input [Jan 1, Jan 1, Jan 8], the Jan1->Jan1 interval (days=0) is
        filtered out but Jan1->Jan8 is valid. One valid interval is enough for
        a result, so compute_prediction should return a non-None result.
        """
        purchases = [
            PurchaseRecord(order_date=date(2025, 1, 1), quantity=2),
            PurchaseRecord(order_date=date(2025, 1, 1), quantity=2),  # same date
            PurchaseRecord(order_date=date(2025, 1, 8), quantity=2),
        ]
        result = compute_prediction(purchases)
        assert result is not None
        assert result["purchase_count"] == 3

    def test_last_purchase_quantity_recorded(self):
        purchases = [
            PurchaseRecord(order_date=date(2025, 1, 1), quantity=3),
            PurchaseRecord(order_date=date(2025, 1, 8), quantity=5),
        ]
        result = compute_prediction(purchases)
        assert result is not None
        assert result["last_purchase_quantity"] == 5


class TestGenerateCandidates:
    """Tests for generate_candidates() shopping list candidate generation."""

    def _make_pred(self, runout_date, confidence=0.8, purchase_count=5, product_id=1):
        """Create a mock ConsumptionPrediction-like object."""
        from unittest.mock import MagicMock
        pred = MagicMock()
        pred.product_id = product_id
        pred.predicted_runout_date = runout_date
        pred.confidence_score = confidence
        pred.purchase_count = purchase_count
        pred.estimated_daily_consumption = 0.3
        pred.avg_quantity_per_purchase = 2.0
        return pred

    LOOKAHEAD = 7
    LEAD_TIME = 7

    def test_includes_product_running_out_today(self):
        today = date(2025, 3, 1)
        pred = self._make_pred(today)
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert any(c.product_id == 1 for c in candidates)

    def test_excludes_product_not_running_out_in_window(self):
        today = date(2025, 3, 1)
        far_future = date(2025, 6, 1)  # 92 days out — well outside 7+7 window
        pred = self._make_pred(far_future)
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert len(candidates) == 0

    def test_excludes_low_confidence_predictions(self):
        today = date(2025, 3, 1)
        pred = self._make_pred(today, confidence=0.1)
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert len(candidates) == 0

    def test_excludes_predictions_with_few_purchases(self):
        today = date(2025, 3, 1)
        pred = self._make_pred(today, purchase_count=2)
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert len(candidates) == 0

    def test_quantity_is_at_least_avg_per_purchase(self):
        today = date(2025, 3, 1)
        pred = self._make_pred(today)
        pred.avg_quantity_per_purchase = 4.0
        pred.estimated_daily_consumption = 0.1  # ceil(0.1*7)=1, which is < 4 → should clamp to 4
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert len(candidates) == 1
        assert candidates[0].quantity >= 4

    def test_reason_includes_runout_date(self):
        today = date(2025, 3, 1)
        pred = self._make_pred(today)
        candidates = generate_candidates([pred], target_date=today, lookahead_days=self.LOOKAHEAD, lead_time_days=self.LEAD_TIME)
        assert len(candidates) == 1
        assert "2025-03-01" in candidates[0].reason
