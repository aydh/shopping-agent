from __future__ import annotations

from collections import namedtuple
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shopping_agent.models import ListStatus, Product, ProductMatch, ShoppingList, ShoppingListItem, Store
from shopping_agent.routes import mcp as mcp_routes
from shopping_agent.routes.views import dashboard, health, orders, predictions, prices, product_lookup, settings, shopping_list
from shopping_agent.services.prediction import PredictionView


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
async def test_dashboard_view_builds_summary_context(monkeypatch, fake_result, dummy_templates, make_request):
    monkeypatch.setattr(dashboard, "templates", dummy_templates)
    order_row = namedtuple("OrderRow", ["store", "count", "last_sync"])
    product_row = namedtuple("ProductRow", ["store", "count"])
    match_row = namedtuple("MatchRow", ["is_rejected", "count"])
    high = PredictionView(
        product_id=1,
        product=_product(1, Store.COLES, "Milk", 4.0),
        predicted_runout_date=date.today(),
        estimated_daily_consumption=0.2,
        confidence_score=0.9,
        last_purchased_date=date.today(),
        last_purchase_store="coles",
        last_purchase_quantity=1,
        days_until_runout=0,
        is_matched=True,
        matched_product=_product(2, Store.WOOLWORTHS, "Milk", 4.5),
        match_id=1,
    )
    low = PredictionView(
        product_id=2,
        product=_product(2, Store.WOOLWORTHS, "Bread", 3.0),
        predicted_runout_date=date.today(),
        estimated_daily_consumption=0.2,
        confidence_score=0.1,
        last_purchased_date=date.today(),
        last_purchase_store="woolworths",
        last_purchase_quantity=1,
        days_until_runout=0,
        is_matched=False,
        matched_product=None,
        match_id=None,
    )
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            fake_result(rows=[order_row(Store.COLES, 3, date(2025, 1, 1)), order_row(Store.WOOLWORTHS, 2, date(2025, 1, 2))]),
            fake_result(rows=[product_row(Store.COLES, 4), product_row(Store.WOOLWORTHS, 5)]),
            fake_result(scalar=6),
            fake_result(rows=[match_row(False, 7), match_row(True, 1)]),
            fake_result(scalar=8),
            fake_result(scalar=9),
        ]
    )
    monkeypatch.setattr(dashboard, "get_predictions_with_match_info", AsyncMock(return_value=[high, low]))
    monkeypatch.setattr(dashboard, "get_shopping_list_context", AsyncMock(return_value={"shopping_list": None}))

    response = await dashboard.dashboard(make_request("/"), session)

    assert response.body.decode() == "template:dashboard.html"
    _, context = dummy_templates.template_calls[-1]
    assert context["runout_count"] == 1
    assert context["matched_count"] == 7
    assert context["shopping_list"] is None


@pytest.mark.asyncio
async def test_orders_predictions_and_product_lookup_views(monkeypatch, fake_result, dummy_templates, make_request):
    monkeypatch.setattr(orders, "templates", dummy_templates)
    monkeypatch.setattr(predictions, "templates", dummy_templates)
    monkeypatch.setattr(product_lookup, "templates", dummy_templates)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[SimpleNamespace(id=1)]))
    orders_response = await orders.orders_page(make_request("/orders"), "coles", session)
    assert orders_response.body.decode() == "template:orders.html"

    monkeypatch.setattr(predictions, "get_predictions_with_match_info", AsyncMock(return_value=["pred"]))
    predictions_response = await predictions.predictions_page(make_request("/predictions"), AsyncMock())
    assert predictions_response.body.decode() == "template:predictions.html"

    lookup_response = await product_lookup.product_lookup_page(make_request("/product-lookup"))
    assert lookup_response.body.decode() == "template:product_lookup.html"


@pytest.mark.asyncio
async def test_health_check_reports_ok_and_failure(monkeypatch):
    monkeypatch.setattr(health, "verify_db_connection", AsyncMock())

    ok_response = await health.health_check()

    assert ok_response.status_code == 200
    assert ok_response.body.decode() == '{"status":"ok"}'

    monkeypatch.setattr(health, "verify_db_connection", AsyncMock(side_effect=RuntimeError("db down")))

    error_response = await health.health_check()

    assert error_response.status_code == 503
    assert error_response.body.decode() == '{"status":"unhealthy"}'


@pytest.mark.asyncio
async def test_prices_view_search_match_page_and_settings_views(monkeypatch, fake_result, dummy_templates, make_request):
    monkeypatch.setattr(prices, "templates", dummy_templates)
    monkeypatch.setattr(settings, "templates", dummy_templates)
    visible_coles = _product(1, Store.COLES, "Milk", 4.0)
    visible_ww = _product(2, Store.WOOLWORTHS, "Milk", 5.0)
    unavailable = _product(3, Store.COLES, "Bread", None)
    hidden = _product(4, Store.WOOLWORTHS, "Hidden", 6.0)
    hidden.is_hidden = True
    match = ProductMatch(id=7, product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual")
    match.product_a = visible_coles
    match.product_b = visible_ww
    rejected = ProductMatch(id=8, product_a_id=3, product_b_id=4, confidence=0.3, match_method="manual", is_rejected=True)
    rejected.product_a = unavailable
    rejected.product_b = hidden
    page_session = AsyncMock()
    page_session.execute = AsyncMock(
        side_effect=[
            fake_result(scalars=[visible_coles, visible_ww, unavailable]),
            fake_result(scalars=[match]),
            fake_result(rows=[(1, date(2025, 1, 1)), (2, date(2025, 1, 2)), (3, date(2025, 1, 3))]),
            fake_result(scalars=[rejected]),
            fake_result(scalars=[hidden]),
            fake_result(rows=[(4, date(2025, 1, 4))]),
            fake_result(scalars=[unavailable]),
        ]
    )

    prices_response = await prices.prices_page(make_request("/prices"), page_session)
    assert prices_response.body.decode() == "template:prices.html"

    product = _product(1, Store.COLES, "Milk", 4.0)
    session = AsyncMock()
    session.get = AsyncMock(return_value=product)

    search_page = await prices.search_match_page(1, make_request("/prices/search"), session)
    assert search_page.body.decode() == "template:search_match.html"

    monkeypatch.setattr(settings, "get_db_counts", AsyncMock(return_value={
        "coles_orders": 1,
        "woolworths_orders": 2,
        "coles_order_items": 3,
        "woolworths_order_items": 4,
        "coles_products": 5,
        "woolworths_products": 6,
        "product_matches": 7,
        "price_history": 8,
        "predictions": 9,
        "shopping_lists": 10,
        "shopping_list_items": 11,
    }))
    monkeypatch.setattr(settings, "coles_scraper", SimpleNamespace(is_authenticated=AsyncMock(return_value=True)))
    monkeypatch.setattr(settings, "woolworths_scraper", SimpleNamespace(is_authenticated=AsyncMock(return_value=False)))

    counts_response = await settings.settings_counts(AsyncMock())
    page_response = await settings.settings_page(make_request("/settings"), AsyncMock())

    assert counts_response.body.decode() == "rendered:_settings_counts.html"
    assert page_response.body.decode() == "template:settings.html"


@pytest.mark.asyncio
async def test_shopping_list_views_render_page_redirect_and_confirm(monkeypatch, fake_result, dummy_templates, make_request):
    monkeypatch.setattr(shopping_list, "templates", dummy_templates)
    monkeypatch.setattr(shopping_list, "get_shopping_list_context", AsyncMock(return_value={"shopping_list": None}))
    monkeypatch.setattr(shopping_list, "get_list_history", AsyncMock(return_value=[]))

    page = await shopping_list.shopping_list_page(make_request("/shopping-list"), AsyncMock())
    assert page.body.decode() == "template:shopping_list.html"

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    missing = await shopping_list.find_match_page(1, make_request("/shopping-list/find-match"), session)
    assert missing.headers["location"] == "/shopping-list"

    product = _product(1, Store.COLES, "Milk", 4.0)
    item = ShoppingListItem(id=1, shopping_list_id=1, product_id=1, quantity=2, coles_price=4.0, chosen_store=Store.COLES)
    item.product = product
    confirmed_list = ShoppingList(id=1, name="Confirmed", target_date=date.today(), status=ListStatus.CONFIRMED, items=[item])
    session = AsyncMock()
    session.execute = AsyncMock(return_value=fake_result(scalars=[confirmed_list]))
    monkeypatch.setattr(shopping_list, "resolve_display_names", AsyncMock(return_value=({1: "Milk"}, {}, {})))

    confirm = await shopping_list.confirm_page(make_request("/confirm"), session)
    assert confirm.body.decode() == "template:confirm.html"


@pytest.mark.asyncio
async def test_mcp_read_only_tools(monkeypatch, async_cm):
    session = AsyncMock()
    prediction_product = _product(1, Store.COLES, "Milk", 4.0)
    prediction = PredictionView(
        product_id=1,
        product=prediction_product,
        predicted_runout_date=date.today(),
        estimated_daily_consumption=0.2,
        confidence_score=0.85,
        last_purchased_date=date.today(),
        last_purchase_store="coles",
        last_purchase_quantity=2,
        days_until_runout=0,
        is_matched=False,
        matched_product=None,
        match_id=None,
    )
    active_item = ShoppingListItem(id=2, shopping_list_id=1, product_id=1, quantity=2, chosen_store=Store.COLES, is_user_added=True)
    active_list = ShoppingList(id=1, name="Active", target_date=date.today(), status=ListStatus.DRAFT, items=[active_item])
    monkeypatch.setattr(mcp_routes, "coles_scraper", SimpleNamespace(is_authenticated=AsyncMock(return_value=True), search_product=AsyncMock(return_value=[SimpleNamespace(store_product_id="c-1", name="Milk", brand=None, current_price=4.0, unit_size="2L", is_available=True)])))
    monkeypatch.setattr(mcp_routes, "woolworths_scraper", SimpleNamespace(is_authenticated=AsyncMock(return_value=False)))
    monkeypatch.setattr(mcp_routes, "async_session", MagicMock(return_value=async_cm(session)))
    monkeypatch.setattr(mcp_routes, "get_predictions_with_match_info", AsyncMock(return_value=[prediction]))
    monkeypatch.setattr(mcp_routes, "get_active_list", AsyncMock(return_value=active_list))
    monkeypatch.setattr(mcp_routes, "get_list_history", AsyncMock(return_value=[{"id": 5, "name": "Past", "created_at": date.today(), "status": ListStatus.ORDERED, "store": Store.COLES, "item_count": 1, "total": 7.5}]))
    monkeypatch.setattr(mcp_routes, "compare_product_prices", AsyncMock(return_value=[SimpleNamespace(product_name="Milk", unit_size="2L", coles_price=4.0, woolworths_price=5.0, cheaper_store=Store.COLES, savings=1.0, match_confidence=0.9, is_confirmed=True)]))

    auth = await mcp_routes.get_auth_status("coles")
    predictions_result = await mcp_routes.get_predictions()
    shopping_list_result = await mcp_routes.get_shopping_list()
    history = await mcp_routes.get_shopping_list_history()
    search_results = await mcp_routes.search_products("milk")
    comparison = await mcp_routes.get_price_comparison(1)

    assert auth["authenticated"] is True
    assert predictions_result[0]["product_name"] == "Milk"
    assert shopping_list_result["item_count"] == 1
    assert history[0]["status"] == "ordered"
    assert any(result.get("store") == "woolworths" and "error" in result for result in search_results)
    assert comparison["cheaper_store"] == "coles"


@pytest.mark.asyncio
async def test_mcp_shopping_list_workflow_tools(monkeypatch, async_cm):
    session = AsyncMock()
    monkeypatch.setattr(mcp_routes, "async_session", MagicMock(return_value=async_cm(session)))
    monkeypatch.setattr(mcp_routes, "get_active_list", AsyncMock(side_effect=[None, SimpleNamespace(id=1, status=ListStatus.DRAFT), SimpleNamespace(id=1, status=ListStatus.CONFIRMED)]))
    monkeypatch.setattr(mcp_routes, "generate_shopping_list", AsyncMock(return_value=SimpleNamespace(id=11, items=[], status=ListStatus.DRAFT)))
    monkeypatch.setattr(mcp_routes, "add_item_to_list", AsyncMock(return_value=SimpleNamespace(id=7, product_id=2, quantity=3, chosen_store=Store.COLES)))
    monkeypatch.setattr(mcp_routes, "update_item_quantity", AsyncMock())
    monkeypatch.setattr(mcp_routes, "remove_item", AsyncMock(return_value=True))
    monkeypatch.setattr(mcp_routes, "assign_cheapest_stores", AsyncMock(return_value=2))
    monkeypatch.setattr(mcp_routes, "confirm_list", AsyncMock(return_value=SimpleNamespace(id=1, status=ListStatus.CONFIRMED)))

    create = await mcp_routes.create_shopping_list(from_predictions=True)
    add = await mcp_routes.add_item_to_shopping_list(2, quantity=3)
    update = await mcp_routes.update_list_item_quantity(7, 4)
    removed = await mcp_routes.remove_list_item(7)
    assigned = await mcp_routes.assign_cheapest_store_to_all()
    confirmed = await mcp_routes.confirm_shopping_list()
    closed = await mcp_routes.close_shopping_list()

    assert create["list_id"] == 11
    assert add["quantity"] == 3
    assert update["quantity"] == 4
    assert removed["removed"] is True
    assert assigned["items_assigned"] == 2
    assert confirmed["status"] == "confirmed"
    assert closed["status"] == "ordered"


@pytest.mark.asyncio
async def test_auth_callback_page_renders_with_supabase_config(monkeypatch, dummy_templates, make_request):
    from shopping_agent.routes.views import auth_callback as auth_callback_view
    monkeypatch.setattr(auth_callback_view, "templates", dummy_templates)
    monkeypatch.setattr(auth_callback_view, "settings", SimpleNamespace(
        supabase_url="https://test.supabase.co",
        supabase_anon_key="test-anon-key",
    ))
    request = make_request("/auth/callback")
    response = await auth_callback_view.auth_callback_page(request)
    assert len(dummy_templates.template_calls) == 1
    name, ctx = dummy_templates.template_calls[0]
    assert name == "auth_callback.html"
    assert ctx["supabase_url"] == "https://test.supabase.co"
    assert ctx["supabase_anon_key"] == "test-anon-key"


@pytest.mark.asyncio
async def test_register_page_renders_with_supabase_config(monkeypatch, dummy_templates, make_request):
    from shopping_agent.routes.views import register as register_view
    monkeypatch.setattr(register_view, "templates", dummy_templates)
    monkeypatch.setattr(register_view, "settings", SimpleNamespace(
        supabase_url="https://test.supabase.co",
        supabase_anon_key="test-anon-key",
    ))
    request = make_request("/register")
    response = await register_view.register_page(request)
    assert len(dummy_templates.template_calls) == 1
    name, ctx = dummy_templates.template_calls[0]
    assert name == "register.html"
    assert ctx["supabase_url"] == "https://test.supabase.co"
    assert ctx["supabase_anon_key"] == "test-anon-key"


@pytest.mark.asyncio
async def test_mcp_sync_refresh_and_matching_tools(monkeypatch, async_cm):
    product = _product(1, Store.COLES, "Milk", 4.0)
    partner = _product(2, Store.WOOLWORTHS, "Milk", 4.5)
    session = AsyncMock()
    session.get = AsyncMock(side_effect=[product, partner, ProductMatch(id=9, product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual"), ProductMatch(id=9, product_a_id=1, product_b_id=2, confidence=0.9, match_method="manual")])
    monkeypatch.setattr(mcp_routes, "async_session", MagicMock(return_value=async_cm(session)))
    monkeypatch.setattr(mcp_routes, "coles_scraper", SimpleNamespace(is_authenticated=AsyncMock(return_value=True), stream_order_history=AsyncMock(return_value=None)))
    async def _stream_orders(limit):
        yield SimpleNamespace(store_order_id="ord-1")
    monkeypatch.setattr(mcp_routes, "coles_scraper", SimpleNamespace(is_authenticated=AsyncMock(return_value=True), stream_order_history=_stream_orders))
    monkeypatch.setattr(mcp_routes, "woolworths_scraper", SimpleNamespace(is_authenticated=AsyncMock(return_value=True)))
    monkeypatch.setattr(mcp_routes, "_sync_orders", AsyncMock(return_value=1))
    monkeypatch.setattr(mcp_routes, "do_price_refresh", AsyncMock(return_value=(3, 4)))
    monkeypatch.setattr(mcp_routes, "_refresh_predictions", AsyncMock(return_value=5))
    monkeypatch.setattr(mcp_routes, "match_unmatched_products", AsyncMock(return_value=6))
    monkeypatch.setattr(mcp_routes, "find_or_create_match", AsyncMock(return_value=SimpleNamespace(id=9, product_a_id=1, product_b_id=2, confidence=0.9, match_method="search", is_confirmed=False, is_rejected=False)))
    monkeypatch.setattr(mcp_routes, "add_to_cart", AsyncMock(return_value={"success": True, "count": 1}))

    cart = await mcp_routes.add_confirmed_list_to_cart("woolworths")
    synced = await mcp_routes.sync_orders("coles", limit=1)
    refreshed = await mcp_routes.refresh_prices("woolworths")
    predictions_result = await mcp_routes.refresh_predictions()
    matched = await mcp_routes.match_products()
    found = await mcp_routes.find_product_match(1)
    confirmed = await mcp_routes.confirm_product_match(9)

    assert cart["success"] is True
    assert synced["new_orders"] == 1
    assert refreshed["updated"] == 3
    assert predictions_result["predictions_updated"] == 5
    assert matched["matches_created"] == 6
    assert found["match_id"] == 9
    assert confirmed["confirmed"] is True


# ---------------------------------------------------------------------------
# MCP OAuth middleware + discovery endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_auth_middleware_rejects_missing_token(monkeypatch):
    import uuid
    from shopping_agent.main import MCPAuthMiddleware
    import shopping_agent.main as main_module

    monkeypatch.setattr(main_module, "settings", SimpleNamespace(
        base_url="https://app.example.com",
    ))

    received = []

    async def downstream(scope, receive, send):
        received.append("called")

    middleware = MCPAuthMiddleware(downstream)
    responses = []

    async def send_fn(msg):
        responses.append(msg)

    scope = {"type": "http", "path": "/mcp/", "headers": []}
    await middleware(scope, None, send_fn)

    assert not received
    start = responses[0]
    assert start["status"] == 401
    www_auth = dict(start["headers"])[b"www-authenticate"].decode()
    assert 'resource_metadata="https://app.example.com/.well-known/oauth-protected-resource"' in www_auth


@pytest.mark.asyncio
async def test_mcp_auth_middleware_rejects_invalid_token(monkeypatch):
    from fastapi import HTTPException
    from shopping_agent.main import MCPAuthMiddleware
    import shopping_agent.main as main_module

    monkeypatch.setattr(main_module, "settings", SimpleNamespace(
        base_url="https://app.example.com",
    ))
    monkeypatch.setattr(main_module, "_decode_token", lambda token: (_ for _ in ()).throw(HTTPException(status_code=401, detail="bad")))

    received = []

    async def downstream(scope, receive, send):
        received.append("called")

    middleware = MCPAuthMiddleware(downstream)
    responses = []

    async def send_fn(msg):
        responses.append(msg)

    scope = {
        "type": "http",
        "path": "/mcp/",
        "headers": [[b"authorization", b"Bearer bad-token"]],
    }
    await middleware(scope, None, send_fn)

    assert not received
    start = responses[0]
    assert start["status"] == 401
    www_auth = dict(start["headers"])[b"www-authenticate"].decode()
    assert 'error="invalid_token"' in www_auth


@pytest.mark.asyncio
async def test_mcp_auth_middleware_allows_valid_token_and_sets_user(monkeypatch):
    import uuid
    from shopping_agent.main import MCPAuthMiddleware
    from shopping_agent.routes.mcp import _mcp_user_id_var
    import shopping_agent.main as main_module

    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(main_module, "settings", SimpleNamespace(
        base_url="https://app.example.com",
    ))
    monkeypatch.setattr(main_module, "_decode_token", lambda token: {"sub": str(user_id), "email": "test@example.com"})
    monkeypatch.setattr(main_module, "_claims_to_user", lambda claims: SimpleNamespace(user_id=user_id))

    captured_user_id = None

    async def downstream(scope, receive, send):
        nonlocal captured_user_id
        captured_user_id = _mcp_user_id_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = MCPAuthMiddleware(downstream)

    async def send_fn(msg):
        pass

    scope = {
        "type": "http",
        "path": "/mcp/",
        "headers": [[b"authorization", b"Bearer valid-token"]],
    }
    await middleware(scope, None, send_fn)

    assert captured_user_id == user_id
    # ContextVar is reset after the request
    assert _mcp_user_id_var.get() is None


@pytest.mark.asyncio
async def test_mcp_auth_middleware_passes_through_non_mcp_paths(monkeypatch):
    from shopping_agent.main import MCPAuthMiddleware
    import shopping_agent.main as main_module

    monkeypatch.setattr(main_module, "settings", SimpleNamespace(base_url="https://app.example.com"))

    received = []

    async def downstream(scope, receive, send):
        received.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = MCPAuthMiddleware(downstream)

    async def send_fn(msg):
        pass

    scope = {"type": "http", "path": "/api/auth/session", "headers": []}
    await middleware(scope, None, send_fn)

    assert received == ["/api/auth/session"]


@pytest.mark.asyncio
async def test_oauth_protected_resource_metadata(monkeypatch):
    import shopping_agent.main as main_module

    monkeypatch.setattr(main_module, "settings", SimpleNamespace(
        base_url="https://app.example.com",
        supabase_url="https://proj.supabase.co",
        mcp_oauth_client_id="my-client-id",
    ))

    result = await main_module.oauth_protected_resource_metadata()

    assert result["resource"] == "https://app.example.com/mcp"
    assert result["authorization_servers"] == ["https://proj.supabase.co"]
    assert result["bearer_methods_supported"] == ["header"]
    assert result["client_id"] == "my-client-id"


@pytest.mark.asyncio
async def test_oauth_protected_resource_metadata_omits_client_id_when_unconfigured(monkeypatch):
    import shopping_agent.main as main_module

    monkeypatch.setattr(main_module, "settings", SimpleNamespace(
        base_url="https://app.example.com",
        supabase_url="https://proj.supabase.co",
        mcp_oauth_client_id="",
    ))

    result = await main_module.oauth_protected_resource_metadata()

    assert "client_id" not in result


@pytest.mark.asyncio
async def test_oauth_consent_page_renders_with_authorization_id(monkeypatch, dummy_templates):
    from shopping_agent.routes.views import oauth_consent as oauth_consent_view
    from starlette.requests import Request

    monkeypatch.setattr(oauth_consent_view, "templates", dummy_templates)
    monkeypatch.setattr(oauth_consent_view, "settings", SimpleNamespace(
        supabase_url="https://test.supabase.co",
        supabase_anon_key="test-anon-key",
    ))

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/oauth/consent",
            "headers": [],
            "query_string": b"authorization_id=test-auth-id-123",
        },
        receive=_receive,
    )

    response = await oauth_consent_view.oauth_consent_page(request)

    assert len(dummy_templates.template_calls) == 1
    name, ctx = dummy_templates.template_calls[0]
    assert name == "oauth_consent.html"
    assert ctx["authorization_id"] == "test-auth-id-123"
    assert ctx["supabase_url"] == "https://test.supabase.co"
    assert ctx["supabase_anon_key"] == "test-anon-key"


@pytest.mark.asyncio
async def test_oauth_consent_page_handles_missing_authorization_id(monkeypatch, dummy_templates):
    from shopping_agent.routes.views import oauth_consent as oauth_consent_view
    from starlette.requests import Request

    monkeypatch.setattr(oauth_consent_view, "templates", dummy_templates)
    monkeypatch.setattr(oauth_consent_view, "settings", SimpleNamespace(
        supabase_url="https://test.supabase.co",
        supabase_anon_key="test-anon-key",
    ))

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/oauth/consent",
            "headers": [],
            "query_string": b"",
        },
        receive=_receive,
    )

    response = await oauth_consent_view.oauth_consent_page(request)

    name, ctx = dummy_templates.template_calls[0]
    assert name == "oauth_consent.html"
    assert ctx["authorization_id"] == ""


@pytest.mark.asyncio
async def test_oauth_authorization_server_metadata(monkeypatch):
    import shopping_agent.main as main_module

    monkeypatch.setattr(main_module, "settings", SimpleNamespace(
        base_url="https://app.example.com",
        supabase_url="https://proj.supabase.co",
        mcp_oauth_client_id="my-client-id",
    ))

    result = await main_module.oauth_authorization_server_metadata()

    assert result["issuer"] == "https://proj.supabase.co"
    assert result["authorization_endpoint"] == "https://proj.supabase.co/auth/v1/authorize"
    assert result["token_endpoint"] == "https://proj.supabase.co/auth/v1/token"
    assert "S256" in result["code_challenge_methods_supported"]
    assert "code" in result["response_types_supported"]


@pytest.mark.asyncio
async def test_authorize_redirect_passes_through_query_params(monkeypatch):
    import shopping_agent.main as main_module
    from starlette.requests import Request

    monkeypatch.setattr(main_module, "settings", SimpleNamespace(
        base_url="https://app.example.com",
        supabase_url="https://proj.supabase.co",
        mcp_oauth_client_id="my-client-id",
    ))

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/authorize",
            "headers": [],
            "query_string": b"response_type=code&client_id=abc&code_challenge=xyz&code_challenge_method=S256",
            "server": ("app.example.com", 443),
            "scheme": "https",
        },
        receive=_receive,
    )

    response = await main_module.authorize_redirect(request)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://proj.supabase.co/auth/v1/authorize?")
    assert "response_type=code" in location
    assert "client_id=abc" in location
    assert "code_challenge=xyz" in location
