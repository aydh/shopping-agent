from __future__ import annotations

import json
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import BackgroundTasks

from shopping_agent.models import ConsumptionPrediction, ListStatus, Order, OrderItem, Product, ProductMatch, ShoppingList, ShoppingListItem, Store
from shopping_agent.scrapers.base import ScrapedOrder, ScrapedOrderItem, ScrapedProduct
from shopping_agent.routes import api_auth, api_cart, api_orders, api_predictions
from shopping_agent.routes.api_prices import charts, matches, product_lookup, products, refresh, search
from shopping_agent.routes.api_shopping_list import candidates, crud, items, stores

_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER = SimpleNamespace(user_id=_USER_ID)


class StreamRequest:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


async def _stream_text(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def _product(product_id: int, store: Store, name: str, price: float | None = None) -> Product:
    return Product(
        id=product_id,
        store=store,
        store_product_id=f"{store.value}-{product_id}",
        name=name,
        current_price=price,
        is_available=True,
    )


@pytest.mark.asyncio
async def test_auth_import_cookies_rejects_large_body():
    too_big = [b"x" * (api_auth._MAX_COOKIE_BODY + 1)]

    response = await api_auth.import_cookies("coles", StreamRequest(too_big), user=_USER)

    assert "too large" in response.body.decode().lower()


@pytest.mark.asyncio
async def test_auth_validate_and_logout_use_store_scrapers(monkeypatch):
    ww_validate = AsyncMock(return_value={"ok": True, "detail": "session valid"})
    ww_logout = AsyncMock()
    mock_scraper = SimpleNamespace(validate_cookies=ww_validate, logout=ww_logout)
    monkeypatch.setattr(api_auth, "get_scraper", lambda user_id, store: mock_scraper)

    validate_response = await api_auth.validate("woolworths", user=_USER)
    logout_response = await api_auth.logout_store("woolworths", user=_USER)

    assert "valid" in validate_response.body.decode().lower()
    assert "not connected" in logout_response.body.decode().lower()
    ww_logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_import_cookies_success(monkeypatch):
    mock_scraper = SimpleNamespace(import_cookies=AsyncMock(return_value=True))
    monkeypatch.setattr(api_auth, "get_scraper", lambda user_id, store: mock_scraper)

    response = await api_auth.import_cookies("coles", StreamRequest([b'[{"name":"sid","value":"x","domain":".coles.com.au","path":"/"}]']), user=_USER)

    assert "Connected" in response.body.decode()


@pytest.mark.asyncio
async def test_cart_add_items_to_cart_renders_success_and_failure(monkeypatch, dummy_templates):
    monkeypatch.setattr(api_cart, "templates", dummy_templates)
    session = AsyncMock()
    monkeypatch.setattr(
        api_cart,
        "add_to_cart",
        AsyncMock(return_value={"success": True, "count": 1, "message": "Added 1/1 items", "cart_url": "https://cart", "failed_item_ids": []}),
    )
    monkeypatch.setattr(api_cart, "get_scraper", lambda user_id, store: AsyncMock())

    response = await api_cart.add_items_to_cart("coles", user=_USER, session=session)

    assert "Added 1/1 items" in response.body.decode()
    assert "Go to Coles" in response.body.decode()

    monkeypatch.setattr(
        api_cart,
        "add_to_cart",
        AsyncMock(return_value={"success": False, "message": "No confirmed list"}),
    )
    error = await api_cart.add_items_to_cart("coles", user=_USER, session=session)
    assert "No confirmed list" in error.body.decode()


@pytest.mark.asyncio
async def test_cart_stream_reports_missing_confirmed_list(monkeypatch, fake_result, async_cm):
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[]))
    session.begin = MagicMock(return_value=async_cm(None))
    monkeypatch.setattr(api_cart, "async_session", MagicMock(return_value=async_cm(session)))
    monkeypatch.setattr(api_cart, "get_scraper", lambda user_id, store: AsyncMock())

    response = await api_cart.add_to_cart_stream("coles", user=_USER)
    payload = await _stream_text(response)

    assert "No confirmed list" in payload


@pytest.mark.asyncio
async def test_cart_stream_processes_items_and_emits_done(monkeypatch, fake_result, async_cm):
    product = _product(1, Store.COLES, "Milk", 4.0)
    item = ShoppingListItem(id=11, shopping_list_id=1, product_id=1, quantity=2, chosen_store=Store.COLES)
    item.product = product
    shopping_list = ShoppingList(id=1, name="Confirmed", target_date=date.today(), status=ListStatus.CONFIRMED, items=[item])
    read_session = AsyncMock()
    read_session.execute = AsyncMock(return_value=fake_result(scalars=[shopping_list]))
    read_session.begin = MagicMock(return_value=async_cm(None))
    write_session = AsyncMock()
    write_session.get = AsyncMock(return_value=item)
    write_session.begin = MagicMock(return_value=async_cm(None))
    sessions = iter([read_session, write_session])
    monkeypatch.setattr(api_cart, "async_session", MagicMock(side_effect=lambda: async_cm(next(sessions))))
    monkeypatch.setattr(api_cart, "_resolve_store_product_id", AsyncMock(return_value="c-1"))
    mock_scraper = SimpleNamespace(add_to_cart=AsyncMock(return_value={"c-1": True}), get_cart_url=AsyncMock(return_value="https://cart"))
    monkeypatch.setattr(api_cart, "get_scraper", lambda user_id, store: mock_scraper)

    response = await api_cart.add_to_cart_stream("coles", user=_USER)
    payload = await _stream_text(response)

    assert '"item_id": 11' in payload
    assert '"succeeded": 1' in payload


@pytest.mark.asyncio
async def test_orders_sync_stream_reports_auth_failure(monkeypatch):
    mock_scraper = SimpleNamespace(is_authenticated=AsyncMock(return_value=False))
    monkeypatch.setattr(api_orders, "get_scraper", lambda user_id, store: mock_scraper)

    response = await api_orders.sync_orders_stream("coles", user=_USER)
    payload = await _stream_text(response)

    assert "Not connected to Coles" in payload


@pytest.mark.asyncio
async def test_orders_sync_stream_emits_progress_and_order(monkeypatch, fake_result, dummy_templates, async_cm):
    monkeypatch.setattr(api_orders, "templates", dummy_templates)

    async def _orders(limit):
        yield ScrapedOrder(
            store_order_id="ord-1",
            order_date=date(2025, 1, 1),
            items=[ScrapedOrderItem(store_product_id="c-1", name="Milk", quantity=1, price_paid=4.0)],
        )

    session = AsyncMock()
    order = Order(id=1, store=Store.COLES, store_order_id="ord-1", order_date=date(2025, 1, 1), total_amount=4.0)
    order.items = []
    session.execute = AsyncMock(return_value=fake_result(scalars=[order]))
    session.begin = MagicMock(return_value=async_cm(None))
    monkeypatch.setattr(api_orders, "async_session", MagicMock(return_value=async_cm(session)))
    mock_scraper = SimpleNamespace(is_authenticated=AsyncMock(return_value=True), stream_order_history=_orders)
    monkeypatch.setattr(api_orders, "get_scraper", lambda user_id, store: mock_scraper)
    monkeypatch.setattr(api_orders, "sync_orders", AsyncMock(return_value=1))

    response = await api_orders.sync_orders_stream("coles", user=_USER)
    payload = await _stream_text(response)

    assert "event: fetching" in payload
    assert "event: progress" in payload
    assert "event: order" in payload
    assert '"new_count": 1' in payload


@pytest.mark.asyncio
async def test_orders_purge_and_get_items_render(monkeypatch, fake_result, dummy_templates):
    monkeypatch.setattr(api_orders, "templates", dummy_templates)
    purge_session = AsyncMock()
    purge_session.execute = AsyncMock(
        side_effect=[
            fake_result(),
            SimpleNamespace(rowcount=3),
            fake_result(),
        ]
    )

    purge_response = await api_orders.purge_store_orders("coles", user=_USER, session=purge_session)
    assert "Purged 3 Coles orders" in purge_response.body.decode()

    product = _product(1, Store.COLES, "Milk", 4.0)
    order = Order(id=2, store=Store.COLES, store_order_id="ord", order_date=date(2025, 1, 1), total_amount=8.0)
    order.items = [OrderItem(id=1, order_id=2, product_id=1, quantity=1, price_paid=4.0)]
    order.items[0].product = product
    items_session = AsyncMock()
    items_session.execute = AsyncMock(return_value=fake_result(scalars=[order]))

    items_response = await api_orders.get_order_items(2, user=_USER, session=items_session)
    assert items_response.body.decode() == "rendered:fragments/_order_items_table.html"


@pytest.mark.asyncio
async def test_predictions_refresh_and_purge(monkeypatch, dummy_templates, async_cm):
    monkeypatch.setattr(api_predictions, "templates", dummy_templates)
    monkeypatch.setattr(api_predictions, "refresh_predictions", AsyncMock(return_value=2))
    monkeypatch.setattr(api_predictions, "_predictions_list", AsyncMock(return_value=["pred"]))
    fresh_session = AsyncMock()
    fresh_session.begin = MagicMock(return_value=async_cm(None))
    monkeypatch.setattr(api_predictions, "async_session", MagicMock(return_value=async_cm(fresh_session)))
    monkeypatch.setattr(api_predictions, "set_rls_claims", AsyncMock())
    session = AsyncMock()
    refresh_response = await api_predictions.refresh(user=_USER, session=session)
    assert refresh_response.body.decode() == "rendered:_predictions_grid.html"

    session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=5))
    purge_response = await api_predictions.purge_predictions(user=_USER, session=session)
    assert "Purged 5 predictions" in purge_response.body.decode()


@pytest.mark.asyncio
async def test_product_image_proxy_uses_cache(monkeypatch):
    monkeypatch.setattr(products, "image_cache", SimpleNamespace(get=AsyncMock(return_value=(b"img", "image/png")), set=AsyncMock()))

    response = await products.image_proxy("https://productimages.coles.com.au/x.jpg")

    assert response.body == b"img"
    assert response.media_type == "image/png"


@pytest.mark.asyncio
async def test_product_image_proxy_turns_404_into_http_exception(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers):
            request = httpx.Request("GET", url)
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("missing", request=request, response=response)

    monkeypatch.setattr(products, "image_cache", SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock()))
    monkeypatch.setattr(products.httpx, "AsyncClient", lambda timeout: Client())

    with pytest.raises(Exception) as exc:
        await products.image_proxy("https://productimages.coles.com.au/missing.jpg")

    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_products_hide_restore_and_purge(fake_result):
    product = _product(1, Store.COLES, "Milk", 4.0)
    session = AsyncMock()
    session.get = AsyncMock(side_effect=[product, product])
    session.execute = AsyncMock(side_effect=[
        fake_result(rows=[(1, 2)]),
        fake_result(rows=[(1, 2)]),
        fake_result(),
        fake_result(),
        fake_result(rows=[(1, 2)]),
        fake_result(rows=[(1, 2)]),
        fake_result(),
        fake_result(),
        fake_result(),
        fake_result(),
        SimpleNamespace(rowcount=4),
    ])

    await products.hide_product(1, user=_USER, session=session)
    await products.restore_product(1, user=_USER, session=session)
    assert session.commit.call_count == 2

    purge_response = await products.purge_products("coles", user=_USER, session=session)
    assert "Purged 4 coles products" in purge_response.body.decode()


@pytest.mark.asyncio
async def test_products_hide_restore_cascade_across_match_chain(fake_result):
    first = _product(1, Store.COLES, "Milk", 4.0)
    session = AsyncMock()
    session.get = AsyncMock(side_effect=[first, first])
    session.execute = AsyncMock(side_effect=[
        fake_result(rows=[(1, 2)]),
        fake_result(rows=[(1, 2), (2, 3)]),
        fake_result(rows=[(2, 3)]),
        fake_result(),
        fake_result(),
        fake_result(rows=[(1, 2)]),
        fake_result(rows=[(1, 2), (2, 3)]),
        fake_result(rows=[(2, 3)]),
        fake_result(),
    ])

    await products.hide_product(1, user=_USER, session=session)
    await products.restore_product(1, user=_USER, session=session)
    assert session.commit.call_count == 2


@pytest.mark.asyncio
async def test_refresh_prices_stream_reports_auth_failure_and_done(monkeypatch):
    mock_scraper = SimpleNamespace(is_authenticated=AsyncMock(return_value=False))
    monkeypatch.setattr(refresh, "_coles_scraper", mock_scraper)

    response = await refresh.refresh_prices_stream("coles", user=_USER)
    payload = await _stream_text(response)

    assert "event: error" in payload
    assert "Not connected" in payload


@pytest.mark.asyncio
async def test_refresh_prices_stream_emits_progress_and_done(monkeypatch):
    mock_scraper = SimpleNamespace(is_authenticated=AsyncMock(return_value=True))
    monkeypatch.setattr(refresh, "_coles_scraper", mock_scraper)
    monkeypatch.setattr(refresh, "do_price_refresh", AsyncMock(return_value=(2, 5)))

    response = await refresh.refresh_prices_stream("coles", user=_USER)
    payload = await _stream_text(response)

    assert "event: done" in payload
    assert '"updated": 2' in payload
    assert '"total": 5' in payload


@pytest.mark.asyncio
async def test_search_match_handles_errors_and_confirms_manual_match(monkeypatch, fake_result, make_request):
    monkeypatch.setattr(search, "templates", dummy_templates := SimpleNamespace(
        TemplateResponse=lambda name, context: None,
    ))
    product = _product(1, Store.COLES, "Milk", 4.0)
    session = AsyncMock()
    session.get = AsyncMock(return_value=product)
    monkeypatch.setattr(search, "woolworths_scraper", SimpleNamespace(search_product=AsyncMock(side_effect=RuntimeError("boom"))))

    error_response = await search.search_match(1, make_request("/"), q="milk", return_to="bad", user=_USER, session=session)
    assert "Search failed" in error_response.body.decode()

    existing_target = None
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[fake_result(scalar=existing_target), fake_result(scalars=[])])
    session.add = MagicMock()
    session.flush = AsyncMock()
    redirect = await search.confirm_search_match(
        source_product_id=1,
        store_product_id="w-1",
        store="woolworths",
        name="Milk",
        brand="Brand",
        unit_size="2L",
        current_price=4.2,
        unit_price=2.1,
        unit_price_measure="L",
        image_url="",
        product_url="",
        return_to="prices",
        user=_USER,
        session=session,
    )
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/prices#unmatched"


@pytest.mark.asyncio
async def test_matches_routes_cover_confirm_manual_and_delete(monkeypatch, fake_result, dummy_templates):
    monkeypatch.setattr(matches, "templates", dummy_templates)
    monkeypatch.setattr(matches, "match_unmatched_products", AsyncMock(return_value=2))
    session = AsyncMock()
    run_response = await matches.run_match_products(user=_USER, session=session)
    assert "2 new matches found" in run_response.body.decode()

    coles_product = _product(1, Store.COLES, "Milk", 4.0)
    ww_product = _product(2, Store.WOOLWORTHS, "Milk", 4.5)
    match = ProductMatch(id=7, product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual")
    match.product_a = coles_product
    match.product_b = ww_product
    session.get = AsyncMock(return_value=match)
    session.execute = AsyncMock(return_value=fake_result(rows=[(1, date(2025, 1, 1)), (2, date(2025, 1, 2))]))
    confirm_response = await matches.confirm_match(7, user=_USER, session=session)
    assert confirm_response.body.decode() == "rendered:_match_row.html"

    session = AsyncMock()
    session.get = AsyncMock(side_effect=[coles_product, ww_product])
    session.execute = AsyncMock(return_value=fake_result(scalars=[]))
    session.add = MagicMock()
    created = await matches.create_manual_match(1, 2, user=_USER, session=session)
    assert created.headers["HX-Refresh"] == "true"

    session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=0))
    deleted = await matches.delete_match(999, user=_USER, session=session)
    assert deleted.status_code == 404


@pytest.mark.asyncio
async def test_product_lookup_search_and_select(monkeypatch, fake_result, make_request, dummy_templates):
    monkeypatch.setattr(product_lookup, "templates", dummy_templates)
    coles_result = ScrapedProduct(store_product_id="c-1", name="Milk", current_price=4.0)
    ww_result = ScrapedProduct(store_product_id="w-1", name="Milk", current_price=4.5)
    monkeypatch.setattr(product_lookup, "coles_scraper", SimpleNamespace(search_product=AsyncMock(return_value=[coles_result])))
    monkeypatch.setattr(product_lookup, "woolworths_scraper", SimpleNamespace(search_product=AsyncMock(return_value=[ww_result])))
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(rows=[]))

    response = await product_lookup.product_lookup_search(make_request("/"), q="milk", session=session)
    assert response.body.decode() == "template:_product_lookup_results.html"

    existing = _product(1, Store.COLES, "Milk", 4.0)
    select_session = AsyncMock()
    select_session.execute = AsyncMock(return_value=fake_result(scalars=[existing]))
    select_session.add = MagicMock()
    response = await product_lookup.product_lookup_select(
        make_request("/"),
        store="coles",
        store_product_id=existing.store_product_id,
        name="Milk",
        brand="Brand",
        unit_size="2L",
        current_price=4.5,
        unit_price=2.25,
        unit_price_measure="L",
        image_url="",
        product_url="",
        session=select_session,
    )
    assert existing.current_price == 4.5
    assert response.body.decode() == "template:_product_lookup_selected.html"


@pytest.mark.asyncio
async def test_charts_routes_return_json_and_empty_when_missing(monkeypatch, fake_result, dummy_templates):
    monkeypatch.setattr(charts, "templates", dummy_templates)
    product = _product(1, Store.COLES, "Milk", 4.0)
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[fake_result(rows=[(1, date(2025, 1, 1), 4.0)]), fake_result(scalars=[product])])

    batch = await charts.product_price_history_batch("1", user=_USER, session=session)
    assert json.loads(batch.body.decode()) == {"1": "rendered:_chart_single.html"}

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    empty = await charts.product_price_history(1, user=_USER, session=session)
    assert empty.body.decode() == ""


@pytest.mark.asyncio
async def test_charts_match_history_routes_render(monkeypatch, fake_result, dummy_templates):
    monkeypatch.setattr(charts, "templates", dummy_templates)
    coles_product = _product(1, Store.COLES, "Milk", 4.0)
    ww_product = _product(2, Store.WOOLWORTHS, "Milk", 4.5)
    match = ProductMatch(id=7, product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual")
    match.product_a = coles_product
    match.product_b = ww_product
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[match]),
            fake_result(rows=[(1, date(2025, 1, 1), 4.0), (2, date(2025, 1, 1), 4.5)]),
        ]
    )

    batch = await charts.price_history_batch("7", user=_USER, session=session)
    assert json.loads(batch.body.decode()) == {"7": "rendered:_chart_match.html"}

    session = AsyncMock()
    session.get = AsyncMock(return_value=match)
    monkeypatch.setattr(charts, "_fetch_match_rows", AsyncMock(return_value=([(date(2025, 1, 1), 4.0)], [(date(2025, 1, 1), 4.5)])))
    single = await charts.price_history(7, user=_USER, session=session)
    assert single.body.decode() == "rendered:_chart_match.html"


@pytest.mark.asyncio
async def test_shopping_list_crud_items_stores_and_candidates_routes(monkeypatch, fake_result, dummy_templates, async_cm):
    monkeypatch.setattr(crud, "templates", dummy_templates)
    monkeypatch.setattr(items, "templates", dummy_templates)
    monkeypatch.setattr(stores, "templates", dummy_templates)
    monkeypatch.setattr(candidates, "templates", dummy_templates)
    monkeypatch.setattr(crud, "_shopping_list_context", AsyncMock(return_value={"shopping_list": None}))
    monkeypatch.setattr(crud, "get_list_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(items, "_shopping_list_context", AsyncMock(return_value={"shopping_list": None}))
    monkeypatch.setattr(stores, "_shopping_list_context", AsyncMock(return_value={"shopping_list": None}))
    monkeypatch.setattr(candidates, "_shopping_list_context", AsyncMock(return_value={"shopping_list": None}))
    monkeypatch.setattr(items, "update_item_quantity", AsyncMock())
    monkeypatch.setattr(items, "update_item_store", AsyncMock())
    monkeypatch.setattr(items, "remove_item", AsyncMock())
    monkeypatch.setattr(items, "set_rls_claims", AsyncMock())
    monkeypatch.setattr(items, "_add_item_to_list", AsyncMock(return_value=None))
    monkeypatch.setattr(items, "_render_item_update_response", AsyncMock(return_value=items.HTMLResponse("rendered:_sl_item.htmlrendered:_sl_totals.htmlrendered:_sl_meta.html")))
    monkeypatch.setattr(items, "_render_full_list_content", AsyncMock(return_value="rendered:_shopping_list_content.html"))
    monkeypatch.setattr(candidates, "generate_shopping_list", AsyncMock())
    inner_session = AsyncMock()
    inner_session.begin = MagicMock(return_value=async_cm(None))
    monkeypatch.setattr(items, "async_session", MagicMock(return_value=async_cm(inner_session)))
    inner_cand_session = AsyncMock()
    inner_cand_session.begin = MagicMock(return_value=async_cm(None))
    monkeypatch.setattr(candidates, "async_session", MagicMock(return_value=async_cm(inner_cand_session)))

    new_session = AsyncMock()
    new_session.execute = AsyncMock(return_value=fake_result(scalars=[]))
    new_session.add = MagicMock()
    new_response = await crud.new_list(target_date=None, session=new_session, user=_USER)
    assert "rendered:_shopping_list_content.html" in new_response.body.decode()

    quantity_response = await items.set_quantity(1, 2, user=_USER, session=AsyncMock())
    assert quantity_response.body.decode() == "rendered:_sl_item.htmlrendered:_sl_totals.htmlrendered:_sl_meta.html"

    add_response = await items.add_product_to_list(1, user=_USER, session=AsyncMock())
    assert "No active list" in add_response.body.decode()

    redirect = await stores.submit_store("coles", user=_USER, session=AsyncMock(execute=AsyncMock(return_value=fake_result(scalars=[]))))
    assert redirect.headers["location"] == "/shopping-list"

    add_preds_session = AsyncMock()
    add_preds_session.execute = AsyncMock(return_value=fake_result(scalars=[]))
    add_predictions_response = await candidates.add_predictions(user=_USER, session=add_preds_session)
    assert add_predictions_response.body.decode() == "rendered:_shopping_list_content.html"

    generate_response = await candidates.generate(user=_USER, session=AsyncMock())
    assert generate_response.body.decode() == "rendered:_shopping_list_content.html"


@pytest.mark.asyncio
async def test_shopping_list_delete_past_list_route(monkeypatch, dummy_templates):
    monkeypatch.setattr(crud, "templates", dummy_templates)
    monkeypatch.setattr(crud, "get_list_history", AsyncMock(return_value=[]))
    past_list = ShoppingList(id=8, name="Past", target_date=date.today(), status=ListStatus.ORDERED)
    session = AsyncMock()
    session.get = AsyncMock(return_value=past_list)

    response = await crud.delete_past_list(8, user=_USER, session=session)

    assert response.body.decode() == "rendered:_past_lists_section.html"
    session.delete.assert_awaited_once_with(past_list)


@pytest.mark.asyncio
async def test_shopping_list_items_copy_and_search_and_store_routes(monkeypatch, fake_result, dummy_templates):
    monkeypatch.setattr(items, "templates", dummy_templates)
    monkeypatch.setattr(stores, "templates", dummy_templates)
    monkeypatch.setattr(items, "_shopping_list_context", AsyncMock(return_value={"shopping_list": None}))
    monkeypatch.setattr(stores, "_shopping_list_context", AsyncMock(return_value={"shopping_list": None}))
    monkeypatch.setattr(items, "_render_full_list_content", AsyncMock(return_value="rendered:_shopping_list_content.html"))
    active = ShoppingList(id=1, name="Active", target_date=date.today(), status=ListStatus.DRAFT)
    source = ShoppingList(id=2, name="Past", target_date=date.today(), status=ListStatus.ORDERED)
    product = _product(1, Store.COLES, "Milk", 5.0)
    partner = _product(2, Store.WOOLWORTHS, "Milk", 4.0)
    src_item = ShoppingListItem(id=3, shopping_list_id=2, product_id=1, quantity=2)
    match = ProductMatch(product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual")
    session = AsyncMock()
    session.get = AsyncMock(return_value=source)
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[active]),
            fake_result(scalars=[src_item]),
            fake_result(scalars=[]),
            fake_result(scalars=[product]),
            fake_result(scalars=[match]),
            fake_result(scalars=[partner]),
        ]
    )
    session.add = MagicMock()

    copy_response = await items.copy_list(2, user=_USER, session=session)
    assert copy_response.body.decode() == "rendered:_shopping_list_content.html"

    search_session = AsyncMock()
    search_session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[active]),
            fake_result(scalars=[1]),
            fake_result(scalars=[partner]),
        ]
    )
    search_response = await items.product_search("mi", user=_USER, session=search_session)
    assert search_response.body.decode() == "rendered:_product_search_results.html"

    store_item = ShoppingListItem(id=9, shopping_list_id=1, product_id=1, quantity=1, chosen_store=Store.COLES)
    store_session = AsyncMock()
    store_session.execute = AsyncMock(side_effect=[fake_result(scalars=[active]), fake_result(scalars=[store_item])])
    set_store_response = await stores.set_all_store("woolworths", user=_USER, session=store_session)
    assert store_item.chosen_store == Store.WOOLWORTHS
    assert set_store_response.body.decode() == "rendered:_shopping_list_content.html"


@pytest.mark.asyncio
async def test_shopping_list_candidates_add_predictions_success(monkeypatch, fake_result, dummy_templates):
    monkeypatch.setattr(candidates, "templates", dummy_templates)
    monkeypatch.setattr(candidates, "_shopping_list_context", AsyncMock(return_value={"shopping_list": None}))
    active = ShoppingList(id=1, name="Active", target_date=date.today(), status=ListStatus.DRAFT)
    product = _product(1, Store.COLES, "Milk", 5.0)
    partner = _product(2, Store.WOOLWORTHS, "Milk", 4.0)
    prediction = SimpleNamespace(product=product)
    match = ProductMatch(product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual")
    match.product_a = product
    match.product_b = partner
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[active]),
            fake_result(scalars=[prediction]),
            fake_result(scalars=[match]),
            fake_result(rows=[]),
        ]
    )
    session.get = AsyncMock(return_value=product)
    session.add = MagicMock()
    monkeypatch.setattr(candidates, "generate_candidates", lambda predictions, target_date, lookahead_days: [SimpleNamespace(product_id=1, quantity=2, reason="Predicted")])

    response = await candidates.add_predictions(user=_USER, session=session)

    assert response.body.decode() == "rendered:_shopping_list_content.html"
