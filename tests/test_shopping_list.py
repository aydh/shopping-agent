"""Tests for shopping_agent.services.shopping_list and price_comparison utilities."""
from unittest.mock import MagicMock

import pytest

from shopping_agent.services.shopping_list import choose_best_store


class TestChooseBestStore:
    """Tests for choose_best_store() store-selection utility."""

    def _store(self, value):
        from shopping_agent.models import Store
        return Store(value)

    def test_picks_coles_when_cheaper(self):
        store = choose_best_store(1.50, 2.00, self._store("coles"))
        assert store.value == "coles"

    def test_picks_woolworths_when_cheaper(self):
        store = choose_best_store(2.00, 1.50, self._store("woolworths"))
        assert store.value == "woolworths"

    def test_picks_coles_when_equal(self):
        store = choose_best_store(1.50, 1.50, self._store("coles"))
        # Equal price: Coles wins (<=)
        assert store.value == "coles"

    def test_falls_back_when_only_coles_price(self):
        store = choose_best_store(1.50, None, self._store("woolworths"))
        assert store.value == "coles"

    def test_falls_back_when_only_woolworths_price(self):
        store = choose_best_store(None, 1.50, self._store("coles"))
        assert store.value == "woolworths"

    def test_uses_fallback_when_no_prices(self):
        store = choose_best_store(None, None, self._store("coles"))
        assert store.value == "coles"
