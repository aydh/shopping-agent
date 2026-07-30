"""Tests for the security-headers middleware (main._SecurityHeadersMiddleware)."""
from __future__ import annotations

import pytest

# Ensure the DB engine (created at import time in database.py) gets a parseable
# URL so `main` imports cleanly even when run in isolation.
import shopping_agent.config as _config

if not _config.settings.database_url:
    _config.settings.database_url = "postgresql+asyncpg://user:pass@localhost:5432/db"

from shopping_agent.main import _build_csp, _SecurityHeadersMiddleware


class _InnerApp:
    """Emits a trivial 200 response, optionally with pre-set headers."""

    def __init__(self, headers: list[tuple[bytes, bytes]] | None = None) -> None:
        self._headers = headers or []

    async def __call__(self, scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": list(self._headers)})
        await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(path: str = "/") -> dict:
    return {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""}


async def _drive(mw, scope) -> dict[str, str]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg: dict) -> None:
        sent.append(msg)

    await mw(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


@pytest.mark.asyncio
async def test_security_headers_present():
    mw = _SecurityHeadersMiddleware(_InnerApp())
    headers = await _drive(mw, _http_scope("/login"))
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in headers["strict-transport-security"]
    assert "default-src 'self'" in headers["content-security-policy"]


@pytest.mark.asyncio
async def test_existing_headers_not_overridden():
    inner = _InnerApp(headers=[(b"content-security-policy", b"default-src 'none'")])
    mw = _SecurityHeadersMiddleware(inner)
    headers = await _drive(mw, _http_scope("/"))
    # A route that sets its own CSP keeps it; the middleware only fills gaps.
    assert headers["content-security-policy"] == "default-src 'none'"
    # Other headers are still added.
    assert headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_non_http_scope_passes_through():
    reached = {"called": False}

    class _Inner:
        async def __call__(self, scope, receive, send):
            reached["called"] = True

    mw = _SecurityHeadersMiddleware(_Inner())

    async def receive() -> dict:
        return {"type": "lifespan.startup"}

    async def send(msg: dict) -> None:
        pass

    await mw({"type": "lifespan"}, receive, send)
    assert reached["called"] is True


def test_csp_includes_supabase_origin(monkeypatch):
    import shopping_agent.main as main_mod

    monkeypatch.setattr(main_mod.settings, "supabase_url", "https://proj.supabase.co")
    csp = _build_csp()
    assert "connect-src 'self' https://proj.supabase.co wss://proj.supabase.co" in csp


def test_csp_key_directives():
    csp = _build_csp()
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "https://cdn.tailwindcss.com" in csp
    assert "https://unpkg.com" in csp
