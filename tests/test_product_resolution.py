import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_get_partner_product_returns_none_when_no_match():
    """Returns None gracefully when no ProductMatch exists."""
    from shopping_agent.services.product_resolution import get_partner_product

    # Mock a session where execute returns a result with no match
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)

    result = await get_partner_product(session, product_id=1, target_store="woolworths")
    assert result is None


@pytest.mark.asyncio
async def test_get_partner_product_returns_none_when_match_partner_missing():
    """Returns None when ProductMatch exists but the partner product is not in DB."""
    from shopping_agent.services.product_resolution import get_partner_product

    mock_match = MagicMock()
    mock_match.product_a_id = 1
    mock_match.product_b_id = 2

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_match

    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.get = AsyncMock(return_value=None)

    result = await get_partner_product(session, product_id=1, target_store="woolworths")
    assert result is None


@pytest.mark.asyncio
async def test_get_partner_product_returns_partner_product():
    """Returns the partner Product when a valid ProductMatch exists."""
    from shopping_agent.services.product_resolution import get_partner_product
    from shopping_agent.models import Store

    mock_match = MagicMock()
    mock_match.product_a_id = 1
    mock_match.product_b_id = 2

    mock_partner = MagicMock()
    mock_partner.id = 2
    mock_partner.store = Store.WOOLWORTHS

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_match

    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.get = AsyncMock(return_value=mock_partner)

    result = await get_partner_product(session, product_id=1, target_store="woolworths")
    assert result is mock_partner


@pytest.mark.asyncio
async def test_get_partner_product_picks_correct_side_when_product_is_b():
    """When the given product_id is product_b_id, the partner is product_a."""
    from shopping_agent.services.product_resolution import get_partner_product
    from shopping_agent.models import Store

    mock_match = MagicMock()
    mock_match.product_a_id = 5
    mock_match.product_b_id = 10

    mock_partner = MagicMock()
    mock_partner.id = 5
    mock_partner.store = Store.COLES

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_match

    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.get = AsyncMock(return_value=mock_partner)

    result = await get_partner_product(session, product_id=10, target_store="coles")
    assert result is mock_partner
    # Confirm we fetched product_a_id=5 (the partner)
    session.get.assert_awaited_once()
    call_args = session.get.call_args
    assert call_args[0][1] == 5
