import pytest
from shopping_agent.models.product import Store


def test_store_from_string_coles_lowercase():
    from shopping_agent.db_helpers import store_from_string
    assert store_from_string("coles") == Store.COLES


def test_store_from_string_woolworths_uppercase():
    from shopping_agent.db_helpers import store_from_string
    assert store_from_string("WOOLWORTHS") == Store.WOOLWORTHS


def test_store_from_string_invalid_raises():
    from shopping_agent.db_helpers import store_from_string
    with pytest.raises(ValueError, match="Unknown store"):
        store_from_string("walmart")


def test_visible_products_query_is_select():
    from shopping_agent.db_helpers import visible_products_query
    from sqlalchemy import Select
    stmt = visible_products_query()
    assert isinstance(stmt, Select)
