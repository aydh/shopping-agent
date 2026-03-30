import pytest
from shopping_agent.scrapers.base import validate_cookie_list


def test_validate_cookie_list_rejects_missing_name():
    cookies = [{"value": "abc", "domain": "coles.com.au"}]
    with pytest.raises(ValueError, match="missing required field 'name'"):
        validate_cookie_list(cookies)


def test_validate_cookie_list_rejects_missing_value():
    cookies = [{"name": "session", "domain": "coles.com.au"}]
    with pytest.raises(ValueError, match="missing required field 'value'"):
        validate_cookie_list(cookies)


def test_validate_cookie_list_accepts_valid():
    cookies = [{"name": "session", "value": "abc123", "domain": "coles.com.au"}]
    result = validate_cookie_list(cookies)
    assert result == cookies


def test_validate_cookie_list_rejects_non_list():
    with pytest.raises(ValueError, match="expected a list"):
        validate_cookie_list({"name": "session"})


def test_validate_cookie_list_empty_list_is_valid():
    result = validate_cookie_list([])
    assert result == []
