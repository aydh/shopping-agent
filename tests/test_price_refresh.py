"""Tests for the price_refresh service."""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


class TestPriceRefreshService:
    """Tests for do_price_refresh() service function."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_products(self):
        """Returns (0, 0) tuple when store has no products."""
        from shopping_agent.services.price_refresh import do_price_refresh
        from shopping_agent.models import Store

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session_ctx)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        session_ctx.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )

        with patch("shopping_agent.services.price_refresh.async_session", return_value=session_ctx):
            updated, total = await do_price_refresh(Store.COLES)

        assert updated == 0
        assert total == 0
