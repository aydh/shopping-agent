import pytest
from shopping_agent.models.product import Store


def test_store_from_string_coles_lowercase():
    from shopping_agent.db_helpers import store_from_string
    assert store_from_string("coles") == Store.COLES


def test_store_from_string_woolworths_uppercase():
    from shopping_agent.db_helpers import store_from_string
    assert store_from_string("WOOLWORTHS") == Store.WOOLWORTHS


def test_store_from_string_invalid_raises():
    from fastapi import HTTPException
    from shopping_agent.db_helpers import store_from_string
    with pytest.raises(HTTPException) as exc_info:
        store_from_string("walmart")
    assert exc_info.value.status_code == 422
    assert "Unknown store" in exc_info.value.detail


def test_visible_products_query_is_select():
    import uuid
    from shopping_agent.db_helpers import visible_products_query
    from sqlalchemy import Select
    stmt = visible_products_query(uuid.uuid4())
    assert isinstance(stmt, Select)
