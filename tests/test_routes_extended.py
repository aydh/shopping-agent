"""Additional route tests targeting low-coverage areas:
- api_auth: /session, /logout, /login, /login-playwright endpoints
- api_shopping_list/crud: new_list, delete_current, close, details, delete_past, purge
- api_shopping_list/items: product_search, add_product, delete_item, copy_list
- api_shopping_list/stores: set_all_store, submit_store, submit_split
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shopping_agent.auth import _TOKEN_CACHE, settings
from shopping_agent.models import (
    ListStatus,
    Product,
    ShoppingList,
    ShoppingListItem,
    Store,
)
from shopping_agent.routes import api_auth
from shopping_agent.routes.api_shopping_list import crud, items, stores

_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_USER = SimpleNamespace(user_id=_USER_ID)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class StreamRequest:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


def _make_request(body: dict | None = None, scheme: str = "https"):
    req = MagicMock()
    req.url.scheme = scheme
    req.json = AsyncMock(return_value=body or {})
    return req


def _product(pid: int, store: Store, name: str, price: float | None = 5.0) -> Product:
    return Product(
        id=pid,
        store=store,
        store_product_id=f"{store.value}-{pid}",
        name=name,
        current_price=price,
        is_available=True,
    )


def _list(lid: int, status: ListStatus = ListStatus.DRAFT) -> ShoppingList:
    sl = ShoppingList(id=lid, name="Shopping - Monday 1st January 2025", status=status)
    sl.user_id = _USER_ID
    return sl


def _item(iid: int, product_id: int, quantity: int = 1) -> ShoppingListItem:
    return ShoppingListItem(
        id=iid,
        shopping_list_id=1,
        product_id=product_id,
        quantity=quantity,
        is_removed=False,
        chosen_store=Store.COLES,
    )


# ---------------------------------------------------------------------------
# api_auth /session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_missing_access_token_returns_400():
    req = _make_request(body={})
    response = await api_auth.set_session(req)
    assert response.status_code == 400
    assert b"access_token" in response.body


@pytest.mark.asyncio
async def test_session_invalid_token_returns_401(monkeypatch):
    _TOKEN_CACHE.cache.clear()

    req = _make_request(body={"access_token": "bad.token.value"})
    response = await api_auth.set_session(req)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_session_valid_token_sets_cookie(monkeypatch):
    from jose import jwt as _jwt

    _TOKEN_CACHE.cache.clear()
    secret = "test-secret-at-least-256-bits-long-for-jose"
    monkeypatch.setattr(settings, "supabase_jwt_secret", secret)

    token = _jwt.encode(
        {"sub": str(_USER_ID), "aud": "authenticated", "email": "t@t.com"},
        secret,
        algorithm="HS256",
    )
    req = _make_request(body={"access_token": token}, scheme="https")
    response = await api_auth.set_session(req)
    assert response.status_code == 200
    assert b'"ok": true' in response.body or b'"ok":true' in response.body
    cookie_header = response.headers.get("set-cookie", "")
    assert "sb-access-token" in cookie_header
    assert "HttpOnly" in cookie_header


def test_is_compact_jwt_rejects_crlf_smuggling():
    """The compact-JWT guard must reject any value carrying CR/LF characters.

    Regression for cookie injection (CWE-20): ``re.match`` with a ``$``-anchored
    pattern accepts a trailing newline (``$`` matches just before a final
    ``\\n``), which would let a control character reach the ``Set-Cookie``
    header. The split + per-segment ``fullmatch`` guard closes that hole.
    """
    valid = "aaa.bbb.ccc"
    assert api_auth._is_compact_jwt(valid)
    assert not api_auth._is_compact_jwt(valid + "\n")
    assert not api_auth._is_compact_jwt(valid + "\r\n")
    assert not api_auth._is_compact_jwt(valid + "\nSet-Cookie: evil=1")
    # Wrong segment count is rejected too.
    assert not api_auth._is_compact_jwt("aaa.bbb")
    assert not api_auth._is_compact_jwt("aaa.bbb.ccc.ddd")


# ---------------------------------------------------------------------------
# api_auth /logout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logout_session_returns_204_and_clears_cookie():
    req = _make_request()
    response = await api_auth.logout_session(req)
    assert response.status_code == 204
    cookie_header = response.headers.get("set-cookie", "")
    assert "sb-access-token" in cookie_header


# ---------------------------------------------------------------------------
# api_auth /login/{store}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_store_returns_instructions():
    response = await api_auth.login("coles")
    assert response.status_code == 200
    assert b"Import Cookies" in response.body


# ---------------------------------------------------------------------------
# api_auth /login-playwright/{store} — unsupported store
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_playwright_unknown_store_raises_422():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await api_auth.login_playwright(
            "unknown_store", email="a@b.com", password="pw", user=_USER
        )
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# api_auth /import-cookies — empty and invalid JSON
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_cookies_empty_array_returns_error():
    response = await api_auth.import_cookies("coles", StreamRequest([b"[]"]), user=_USER)
    assert "cannot be empty" in response.body.decode().lower()


@pytest.mark.asyncio
async def test_import_cookies_invalid_json_returns_error():
    response = await api_auth.import_cookies("coles", StreamRequest([b"not json"]), user=_USER)
    assert "invalid json" in response.body.decode().lower()


@pytest.mark.asyncio
async def test_import_cookies_not_list_returns_error():
    response = await api_auth.import_cookies("coles", StreamRequest([b'{"name":"x"}']), user=_USER)
    assert "must be a json array" in response.body.decode().lower()


@pytest.mark.asyncio
async def test_import_cookies_unknown_store_raises_422():
    from fastapi import HTTPException
    valid_cookie = b'[{"name":"sid","value":"v","domain":".x.com","path":"/"}]'
    with pytest.raises(HTTPException) as exc_info:
        await api_auth.import_cookies("badstore", StreamRequest([valid_cookie]), user=_USER)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_import_cookies_scraper_returns_false(monkeypatch):
    mock_scraper = SimpleNamespace(import_cookies=AsyncMock(return_value=False))
    monkeypatch.setattr(api_auth, "get_scraper", lambda uid, store: mock_scraper)
    valid_cookie = b'[{"name":"sid","value":"v","domain":".coles.com.au","path":"/"}]'

    response = await api_auth.import_cookies("coles", StreamRequest([valid_cookie]), user=_USER)
    assert "invalid" in response.body.decode().lower()


# ---------------------------------------------------------------------------
# crud._ordinal and _list_name helpers
# ---------------------------------------------------------------------------

def test_ordinal_st():
    assert crud._ordinal(1) == "1st"
    assert crud._ordinal(21) == "21st"


def test_ordinal_nd():
    assert crud._ordinal(2) == "2nd"
    assert crud._ordinal(22) == "22nd"


def test_ordinal_rd():
    assert crud._ordinal(3) == "3rd"
    assert crud._ordinal(23) == "23rd"


def test_ordinal_th():
    assert crud._ordinal(11) == "11th"
    assert crud._ordinal(12) == "12th"
    assert crud._ordinal(13) == "13th"
    assert crud._ordinal(4) == "4th"


def test_list_name_format():
    from datetime import date
    name = crud._list_name(date(2025, 1, 6))
    assert "Monday" in name
    assert "6th" in name
    assert "January 2025" in name


# ---------------------------------------------------------------------------
# crud.new_list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_list_when_existing_returns_empty(monkeypatch, dummy_templates):
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=_list(1)))))
    )
    monkeypatch.setattr(crud, "templates", dummy_templates)

    response = await crud.new_list(target_date=None, user=_USER, session=session)
    assert response.body == b""


@pytest.mark.asyncio
async def test_new_list_creates_new_list(monkeypatch, dummy_templates):
    from datetime import date
    shopping_list = _list(1)
    session = AsyncMock()
    # First execute: no existing list; second execute: context query
    first_result = MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None))))
    session.execute = AsyncMock(return_value=first_result)
    session.flush = AsyncMock()
    session.add = MagicMock()

    monkeypatch.setattr(crud, "templates", dummy_templates)
    monkeypatch.setattr(
        crud, "_shopping_list_context",
        AsyncMock(return_value={"shopping_list": shopping_list, "items": [], "store_metrics": {}, "active_item_count": 0})
    )

    response = await crud.new_list(target_date=date(2025, 1, 6), user=_USER, session=session)
    session.add.assert_called_once()
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# crud.delete_current_list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_current_list_no_list(monkeypatch, dummy_templates):
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None))))
    )
    monkeypatch.setattr(crud, "templates", dummy_templates)
    monkeypatch.setattr(
        crud, "_shopping_list_context",
        AsyncMock(return_value={"shopping_list": None, "items": [], "store_metrics": {}, "active_item_count": 0})
    )

    response = await crud.delete_current_list(user=_USER, session=session)
    assert response.status_code == 200
    session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_current_list_deletes_items_and_list(monkeypatch, dummy_templates):
    sl = _list(5)
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=sl))))
    )
    monkeypatch.setattr(crud, "templates", dummy_templates)
    monkeypatch.setattr(
        crud, "_shopping_list_context",
        AsyncMock(return_value={"shopping_list": None, "items": [], "store_metrics": {}, "active_item_count": 0})
    )

    response = await crud.delete_current_list(user=_USER, session=session)
    session.delete.assert_called_once_with(sl)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# crud.close_list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_list_marks_ordered():
    sl = _list(3, status=ListStatus.DRAFT)
    session = AsyncMock()
    session.get = AsyncMock(return_value=sl)
    session.commit = AsyncMock()

    response = await crud.close_list(3, user=_USER, session=session)
    assert sl.status == ListStatus.ORDERED
    assert response.status_code == 303
    assert response.headers["location"] == "/shopping-list"


@pytest.mark.asyncio
async def test_close_list_not_found_still_redirects():
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    response = await crud.close_list(99, user=_USER, session=session)
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# crud.list_details
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_details_returns_html(monkeypatch, dummy_templates):
    p = _product(1, Store.COLES, "Milk")
    item = _item(1, product_id=1)

    items_result = MagicMock()
    items_result.scalars.return_value.all.return_value = [item]
    products_result = MagicMock()
    products_result.scalars.return_value.all.return_value = [p]

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[items_result, products_result])
    monkeypatch.setattr(crud, "templates", dummy_templates)

    response = await crud.list_details(1, user=_USER, session=session)
    assert response.status_code == 200
    assert dummy_templates.render_calls[0][0] == "_past_list_details.html"
    items_data = dummy_templates.render_calls[0][1]["items"]
    assert len(items_data) == 1
    assert items_data[0]["name"] == "Milk"


# ---------------------------------------------------------------------------
# crud.delete_past_list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_past_list_ordered_list(monkeypatch, dummy_templates):
    sl = _list(7, status=ListStatus.ORDERED)
    session = AsyncMock()
    session.get = AsyncMock(return_value=sl)
    monkeypatch.setattr(crud, "templates", dummy_templates)
    monkeypatch.setattr(crud, "get_list_history", AsyncMock(return_value=[]))

    response = await crud.delete_past_list(7, user=_USER, session=session)
    session.delete.assert_called_once_with(sl)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_delete_past_list_draft_list_not_deleted(monkeypatch, dummy_templates):
    sl = _list(8, status=ListStatus.DRAFT)
    session = AsyncMock()
    session.get = AsyncMock(return_value=sl)
    monkeypatch.setattr(crud, "templates", dummy_templates)
    monkeypatch.setattr(crud, "get_list_history", AsyncMock(return_value=[]))

    response = await crud.delete_past_list(8, user=_USER, session=session)
    session.delete.assert_not_called()
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# crud.purge_shopping_lists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_purge_shopping_lists():
    session = AsyncMock()
    items_exec = MagicMock()
    items_exec.rowcount = 5
    lists_exec = MagicMock()
    lists_exec.rowcount = 2
    session.execute = AsyncMock(side_effect=[items_exec, lists_exec])
    session.commit = AsyncMock()

    response = await crud.purge_shopping_lists(user=_USER, session=session)
    assert "2 lists" in response.body.decode()
    assert "5 items" in response.body.decode()


# ---------------------------------------------------------------------------
# items.product_search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_product_search_short_query_returns_empty():
    session = AsyncMock()
    response = await items.product_search(q="a", user=_USER, session=session)
    assert response.body == b""


@pytest.mark.asyncio
async def test_product_search_returns_results(monkeypatch, dummy_templates):
    p = _product(1, Store.COLES, "Milk 2L")
    active_list = _list(1)

    list_result = MagicMock()
    list_result.scalars.return_value.first.return_value = active_list
    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = []
    products_result = MagicMock()
    products_result.scalars.return_value.all.return_value = [p]

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[list_result, existing_result, products_result])
    monkeypatch.setattr(items, "templates", dummy_templates)

    response = await items.product_search(q="milk", user=_USER, session=session)
    assert response.status_code == 200
    assert dummy_templates.render_calls[0][0] == "_product_search_results.html"


@pytest.mark.asyncio
async def test_product_search_no_active_list(monkeypatch, dummy_templates):
    p = _product(1, Store.COLES, "Eggs")

    list_result = MagicMock()
    list_result.scalars.return_value.first.return_value = None
    products_result = MagicMock()
    products_result.scalars.return_value.all.return_value = [p]

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[list_result, products_result])
    monkeypatch.setattr(items, "templates", dummy_templates)

    response = await items.product_search(q="egg", user=_USER, session=session)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# items.add_product_to_list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_product_no_active_list(monkeypatch, dummy_templates):
    monkeypatch.setattr(items, "_add_item_to_list", AsyncMock(return_value=None))
    monkeypatch.setattr(items, "templates", dummy_templates)
    session = AsyncMock()

    response = await items.add_product_to_list(product_id=1, user=_USER, session=session)
    assert "no active list" in response.body.decode().lower()


@pytest.mark.asyncio
async def test_add_product_new_item_shows_added(monkeypatch, dummy_templates):
    item = _item(1, product_id=1, quantity=1)
    monkeypatch.setattr(items, "_add_item_to_list", AsyncMock(return_value=item))
    monkeypatch.setattr(items, "templates", dummy_templates)

    fake_cm = AsyncMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_cm)
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    fake_cm.begin = MagicMock(return_value=fake_cm)
    monkeypatch.setattr(items, "async_session", MagicMock(return_value=fake_cm))
    monkeypatch.setattr(items, "set_rls_claims", AsyncMock())
    monkeypatch.setattr(items, "_render_full_list_content", AsyncMock(return_value="<ul></ul>"))

    session = AsyncMock()
    response = await items.add_product_to_list(product_id=1, user=_USER, session=session)
    assert "Added" in response.body.decode()


@pytest.mark.asyncio
async def test_add_product_existing_item_shows_qty_updated(monkeypatch, dummy_templates):
    item = _item(1, product_id=1, quantity=3)
    monkeypatch.setattr(items, "_add_item_to_list", AsyncMock(return_value=item))
    monkeypatch.setattr(items, "templates", dummy_templates)

    fake_cm = AsyncMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_cm)
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    fake_cm.begin = MagicMock(return_value=fake_cm)
    monkeypatch.setattr(items, "async_session", MagicMock(return_value=fake_cm))
    monkeypatch.setattr(items, "set_rls_claims", AsyncMock())
    monkeypatch.setattr(items, "_render_full_list_content", AsyncMock(return_value="<ul></ul>"))

    session = AsyncMock()
    response = await items.add_product_to_list(product_id=1, user=_USER, session=session)
    assert "Qty updated" in response.body.decode()


# ---------------------------------------------------------------------------
# items.copy_list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_copy_list_no_active_list(monkeypatch):
    session = AsyncMock()
    no_list_result = MagicMock()
    no_list_result.scalars.return_value.first.return_value = None
    session.execute = AsyncMock(return_value=no_list_result)

    response = await items.copy_list(source_list_id=2, user=_USER, session=session)
    assert "no active list" in response.body.decode().lower()


@pytest.mark.asyncio
async def test_copy_list_same_list_returns_error(monkeypatch):
    active = _list(5)
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = active
    session.execute = AsyncMock(return_value=result)

    response = await items.copy_list(source_list_id=5, user=_USER, session=session)
    assert "cannot copy" in response.body.decode().lower()


@pytest.mark.asyncio
async def test_copy_list_source_not_found(monkeypatch):
    active = _list(5)
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = active
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=None)

    response = await items.copy_list(source_list_id=99, user=_USER, session=session)
    assert "not found" in response.body.decode().lower()


@pytest.mark.asyncio
async def test_copy_list_source_not_ordered(monkeypatch):
    active = _list(5)
    source = _list(6, status=ListStatus.DRAFT)
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = active
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=source)

    response = await items.copy_list(source_list_id=6, user=_USER, session=session)
    assert "not found" in response.body.decode().lower()


# ---------------------------------------------------------------------------
# stores.set_all_store
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_all_store_no_list(monkeypatch, dummy_templates):
    session = AsyncMock()
    no_list = MagicMock()
    no_list.scalars.return_value.first.return_value = None
    session.execute = AsyncMock(return_value=no_list)
    monkeypatch.setattr(stores, "templates", dummy_templates)
    monkeypatch.setattr(
        stores, "_shopping_list_context",
        AsyncMock(return_value={"shopping_list": None, "items": [], "store_metrics": {}, "active_item_count": 0})
    )

    response = await stores.set_all_store("coles", user=_USER, session=session)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_set_all_store_updates_items(monkeypatch, dummy_templates):
    sl = _list(1)
    item1 = _item(1, product_id=1)
    item2 = _item(2, product_id=2)
    item1.chosen_store = Store.COLES
    item2.chosen_store = Store.COLES

    results = [
        MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=sl)))),
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[item1, item2])))),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    monkeypatch.setattr(stores, "templates", dummy_templates)
    monkeypatch.setattr(
        stores, "_shopping_list_context",
        AsyncMock(return_value={"shopping_list": sl, "items": [item1, item2], "store_metrics": {}, "active_item_count": 2})
    )

    response = await stores.set_all_store("woolworths", user=_USER, session=session)
    assert item1.chosen_store == Store.WOOLWORTHS
    assert item2.chosen_store == Store.WOOLWORTHS
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# stores.submit_store
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_store_no_list_redirects(monkeypatch):
    session = AsyncMock()
    no_list = MagicMock()
    no_list.scalars.return_value.first.return_value = None
    session.execute = AsyncMock(return_value=no_list)

    response = await stores.submit_store("coles", user=_USER, session=session)
    assert response.status_code == 303
    assert response.headers["location"] == "/shopping-list"


@pytest.mark.asyncio
async def test_submit_store_confirms_and_redirects():
    sl = _list(1, status=ListStatus.DRAFT)
    item = _item(1, product_id=1)

    results = [
        MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=sl)))),
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[item])))),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    session.commit = AsyncMock()

    response = await stores.submit_store("woolworths", user=_USER, session=session)
    assert sl.status == ListStatus.CONFIRMED
    assert item.chosen_store == Store.WOOLWORTHS
    assert response.status_code == 303
    assert response.headers["location"] == "/confirm"


# ---------------------------------------------------------------------------
# stores.submit_split
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_split_no_list_redirects(monkeypatch):
    session = AsyncMock()
    no_list = MagicMock()
    no_list.scalars.return_value.first.return_value = None
    session.execute = AsyncMock(return_value=no_list)

    response = await stores.submit_split(user=_USER, session=session)
    assert response.status_code == 303
    assert response.headers["location"] == "/shopping-list"


@pytest.mark.asyncio
async def test_submit_split_assigns_cheapest_and_confirms(monkeypatch):
    sl = _list(2, status=ListStatus.DRAFT)
    updated_sl = _list(2, status=ListStatus.DRAFT)

    session = AsyncMock()
    list_result = MagicMock()
    list_result.scalars.return_value.first.return_value = sl
    session.execute = AsyncMock(return_value=list_result)

    monkeypatch.setattr(stores, "assign_cheapest_stores", AsyncMock())

    fake_begin = AsyncMock()
    fake_begin.__aenter__ = AsyncMock(return_value=fake_begin)
    fake_begin.__aexit__ = AsyncMock(return_value=False)
    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.begin = MagicMock(return_value=fake_begin)
    fake_session.get = AsyncMock(return_value=updated_sl)
    monkeypatch.setattr(stores, "async_session", MagicMock(return_value=fake_session))
    monkeypatch.setattr(stores, "set_rls_claims", AsyncMock())

    response = await stores.submit_split(user=_USER, session=session)
    assert updated_sl.status == ListStatus.CONFIRMED
    assert response.status_code == 303
    assert response.headers["location"] == "/confirm"
