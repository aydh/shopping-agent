"""Tests for the global auth-gate middleware (main._AuthGateMiddleware)."""
from __future__ import annotations

import uuid

import pytest
from jose import jwt

# Ensure the DB engine (created at import time in database.py) gets a parseable
# URL so `main` imports cleanly even when run in isolation.
import shopping_agent.config as _config

if not _config.settings.database_url:
    _config.settings.database_url = "postgresql+asyncpg://user:pass@localhost:5432/db"

from shopping_agent.main import _AuthGateMiddleware

_JWT_SECRET = "test-secret-at-least-256-bits-long-for-jose"


def _make_hs256_token() -> str:
    payload = {
        "sub": str(uuid.uuid4()),
        "email": "test@example.com",
        "aud": "authenticated",
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm="HS256")


class _InnerApp:
    """Records whether it was reached and emits a trivial 200 response."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers or [],
        "query_string": b"",
    }


async def _drive(mw, scope) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg: dict) -> None:
        sent.append(msg)

    await mw(scope, receive, send)
    return sent


def _status(sent: list[dict]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _location(sent: list[dict]) -> str | None:
    start = next(m for m in sent if m["type"] == "http.response.start")
    for key, value in start["headers"]:
        if key.lower() == b"location":
            return value.decode()
    return None


# ---------------------------------------------------------------------------
# _is_public classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/login",
        "/register",
        "/auth/callback",
        "/oauth/consent",
        "/healthz",
        "/authorize",
        "/static/app.css",
        "/.well-known/oauth-protected-resource",
        "/mcp",
        "/mcp/",
        "/mcp/messages",
    ],
)
def test_public_paths_are_public(path):
    mw = _AuthGateMiddleware(_InnerApp())
    assert mw._is_public(path) is True


@pytest.mark.parametrize(
    "path",
    ["/", "/orders", "/prices", "/settings", "/api/prices/search", "/mcpfoo"],
)
def test_protected_paths_are_not_public(path):
    mw = _AuthGateMiddleware(_InnerApp())
    assert mw._is_public(path) is False


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_public_path_passes_through():
    inner = _InnerApp()
    mw = _AuthGateMiddleware(inner)
    sent = await _drive(mw, _http_scope("/login"))
    assert inner.called is True
    assert _status(sent) == 200


@pytest.mark.asyncio
async def test_non_http_scope_passes_through():
    inner = _InnerApp()
    mw = _AuthGateMiddleware(inner)
    scope = {"type": "lifespan"}

    received: list = []

    async def receive() -> dict:
        return {"type": "lifespan.startup"}

    async def send(msg: dict) -> None:
        received.append(msg)

    await mw(scope, receive, send)
    assert inner.called is True


@pytest.mark.asyncio
async def test_unauthenticated_page_redirects_to_login():
    inner = _InnerApp()
    mw = _AuthGateMiddleware(inner)
    sent = await _drive(mw, _http_scope("/"))
    assert inner.called is False
    assert _status(sent) == 307
    assert _location(sent) == "/login"


@pytest.mark.asyncio
async def test_unauthenticated_api_returns_401():
    inner = _InnerApp()
    mw = _AuthGateMiddleware(inner)
    sent = await _drive(mw, _http_scope("/api/prices/search"))
    assert inner.called is False
    assert _status(sent) == 401


@pytest.mark.asyncio
async def test_authenticated_cookie_passes_through(monkeypatch):
    import shopping_agent.auth as auth_mod
    from shopping_agent.auth import _TOKEN_CACHE

    _TOKEN_CACHE.cache.clear()
    monkeypatch.setattr(auth_mod.settings, "supabase_jwt_secret", _JWT_SECRET)

    token = _make_hs256_token()
    inner = _InnerApp()
    mw = _AuthGateMiddleware(inner)
    headers = [(b"cookie", f"sb-access-token={token}".encode())]
    sent = await _drive(mw, _http_scope("/", headers))
    assert inner.called is True
    assert _status(sent) == 200


@pytest.mark.asyncio
async def test_authenticated_bearer_passes_through(monkeypatch):
    import shopping_agent.auth as auth_mod
    from shopping_agent.auth import _TOKEN_CACHE

    _TOKEN_CACHE.cache.clear()
    monkeypatch.setattr(auth_mod.settings, "supabase_jwt_secret", _JWT_SECRET)

    token = _make_hs256_token()
    inner = _InnerApp()
    mw = _AuthGateMiddleware(inner)
    headers = [(b"authorization", f"Bearer {token}".encode())]
    sent = await _drive(mw, _http_scope("/api/prices/search", headers))
    assert inner.called is True
    assert _status(sent) == 200
