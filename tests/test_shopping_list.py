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


from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from shopping_agent.models import Store


class TestAssignCheapestStores:
    """Tests for assign_cheapest_stores() service function."""

    @pytest.mark.asyncio
    async def test_assigns_cheapest_store_per_item(self):
        """Items get assigned to whichever store is cheaper."""
        from shopping_agent.services.shopping_list import assign_cheapest_stores
        from shopping_agent.models import ShoppingListItem, ShoppingList, ListStatus

        item_coles_cheaper = MagicMock(spec=ShoppingListItem)
        item_coles_cheaper.coles_price = 1.50
        item_coles_cheaper.woolworths_price = 2.00
        item_coles_cheaper.chosen_store = Store.WOOLWORTHS
        item_coles_cheaper.is_removed = False

        item_ww_cheaper = MagicMock(spec=ShoppingListItem)
        item_ww_cheaper.coles_price = 3.00
        item_ww_cheaper.woolworths_price = 2.50
        item_ww_cheaper.chosen_store = Store.COLES
        item_ww_cheaper.is_removed = False

        shopping_list = MagicMock(spec=ShoppingList)
        shopping_list.id = 1
        shopping_list.status = ListStatus.DRAFT

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[
            MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=shopping_list)))),
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[item_coles_cheaper, item_ww_cheaper])))),
        ])
        session.commit = AsyncMock()

        import uuid
        user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        result = await assign_cheapest_stores(session, user_id)

        assert result == 2
        assert item_coles_cheaper.chosen_store == Store.COLES
        assert item_ww_cheaper.chosen_store == Store.WOOLWORTHS
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_active_list(self):
        """Returns 0 when no active shopping list exists."""
        from shopping_agent.services.shopping_list import assign_cheapest_stores

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        ))

        import uuid
        user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        result = await assign_cheapest_stores(session, user_id)
        assert result == 0


class TestAddItemToList:
    """Tests for add_item_to_list() service function."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_active_list(self):
        """Returns None if no active shopping list exists."""
        from shopping_agent.services.shopping_list import add_item_to_list

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        ))

        import uuid
        user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        result = await add_item_to_list(session, user_id, product_id=1)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_product_not_found(self):
        """Returns None if product_id doesn't exist."""
        from shopping_agent.services.shopping_list import add_item_to_list
        from shopping_agent.models import ShoppingList, ListStatus

        shopping_list = MagicMock(spec=ShoppingList)
        shopping_list.id = 1
        shopping_list.status = ListStatus.DRAFT

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=shopping_list)))
        ))
        session.get = AsyncMock(return_value=None)  # product not found

        import uuid
        user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        result = await add_item_to_list(session, user_id, product_id=999)
        assert result is None
