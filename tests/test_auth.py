"""Tests for auth.py: _TTLCache, _decode_token, _claims_to_user, get_current_user*."""
from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from jose import jwt

from shopping_agent.auth import (
    CurrentUser,
    _TTLCache,
    _claims_to_user,
    _decode_token,
    get_current_user,
    get_current_user_from_cookie,
)


# ---------------------------------------------------------------------------
# _TTLCache
# ---------------------------------------------------------------------------

def test_ttlcache_miss_returns_none():
    cache: _TTLCache[str] = _TTLCache(ttl_seconds=60)
    assert cache.get("missing") is None


def test_ttlcache_hit_returns_value():
    cache: _TTLCache[str] = _TTLCache(ttl_seconds=60)
    cache.set("k", "v")
    assert cache.get("k") == "v"


def test_ttlcache_expired_entry_returns_none(monkeypatch):
    cache: _TTLCache[str] = _TTLCache(ttl_seconds=10)
    cache.set("k", "v")
    _real_time = time.time
    monkeypatch.setattr(time, "time", lambda: _real_time() + 20)
    assert cache.get("k") is None


def test_ttlcache_update_overwrites_existing_key():
    cache: _TTLCache[int] = _TTLCache(ttl_seconds=60)
    cache.set("k", 1)
    cache.set("k", 2)
    assert cache.get("k") == 2
    assert len(cache.cache) == 1


def test_ttlcache_max_size_evicts_oldest():
    cache: _TTLCache[int] = _TTLCache(ttl_seconds=60, max_size=3)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    cache.set("d", 4)  # should evict "a"
    assert cache.get("a") is None
    assert cache.get("d") == 4


# ---------------------------------------------------------------------------
# _decode_token — HS256
# ---------------------------------------------------------------------------

_JWT_SECRET = "test-secret-at-least-256-bits-long-for-jose"


def _make_hs256_token(claims: dict | None = None) -> str:
    payload = {
        "sub": str(uuid.uuid4()),
        "email": "test@example.com",
        "aud": "authenticated",
        **(claims or {}),
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm="HS256")


def test_decode_token_hs256_valid(monkeypatch):
    from shopping_agent.auth import _TOKEN_CACHE
    _TOKEN_CACHE.cache.clear()

    import shopping_agent.auth as auth_mod
    monkeypatch.setattr(auth_mod.settings, "supabase_jwt_secret", _JWT_SECRET)

    token = _make_hs256_token()
    claims = _decode_token(token)
    assert "sub" in claims
    assert claims["aud"] == "authenticated"


def test_decode_token_hs256_cached(monkeypatch):
    from shopping_agent.auth import _TOKEN_CACHE
    _TOKEN_CACHE.cache.clear()

    import shopping_agent.auth as auth_mod
    monkeypatch.setattr(auth_mod.settings, "supabase_jwt_secret", _JWT_SECRET)

    token = _make_hs256_token()
    c1 = _decode_token(token)
    c2 = _decode_token(token)
    assert c1 is c2  # same object — came from cache


def test_decode_token_hs256_missing_secret_raises(monkeypatch):
    import shopping_agent.auth as auth_mod
    monkeypatch.setattr(auth_mod.settings, "supabase_jwt_secret", None)

    token = _make_hs256_token()
    with pytest.raises(HTTPException) as exc_info:
        _decode_token(token)
    assert exc_info.value.status_code == 503


def test_decode_token_invalid_header_raises():
    with pytest.raises(HTTPException) as exc_info:
        _decode_token("not.a.jwt")
    assert exc_info.value.status_code == 401


def test_decode_token_unsupported_alg(monkeypatch):
    """Token whose alg header is something unexpected triggers 401."""
    import shopping_agent.auth as auth_mod
    monkeypatch.setattr(auth_mod.settings, "supabase_jwt_secret", _JWT_SECRET)

    # Manually craft a token with RS256 header but HS256 body (will fail RS256 path)
    # Just test the alg-not-recognised branch by patching get_unverified_header.
    bad_header = {"alg": "NONE", "typ": "JWT"}
    with patch("shopping_agent.auth.jwt.get_unverified_header", return_value=bad_header):
        with pytest.raises(HTTPException) as exc_info:
            _decode_token("any.token.here")
        assert exc_info.value.status_code == 401


def test_decode_token_expired_jwt_raises(monkeypatch):
    import shopping_agent.auth as auth_mod
    from shopping_agent.auth import _TOKEN_CACHE
    _TOKEN_CACHE.cache.clear()

    monkeypatch.setattr(auth_mod.settings, "supabase_jwt_secret", _JWT_SECRET)
    token = _make_hs256_token({"exp": 1})  # already expired
    with pytest.raises(HTTPException) as exc_info:
        _decode_token(token)
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# _claims_to_user
# ---------------------------------------------------------------------------

def test_claims_to_user_valid():
    uid = str(uuid.uuid4())
    user = _claims_to_user({"sub": uid, "email": "a@b.com"})
    assert user.user_id == uuid.UUID(uid)
    assert user.email == "a@b.com"


def test_claims_to_user_missing_sub_raises():
    with pytest.raises(HTTPException) as exc_info:
        _claims_to_user({"email": "a@b.com"})
    assert exc_info.value.status_code == 401


def test_current_user_stores_raw_claims():
    uid = str(uuid.uuid4())
    claims = {"sub": uid, "email": "x@y.com", "role": "authenticated"}
    user = _claims_to_user(claims)
    assert user.raw_claims is claims


# ---------------------------------------------------------------------------
# get_current_user_from_cookie
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_current_user_from_cookie_no_cookie_redirects(make_request):
    request = make_request("/")
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_from_cookie(request)
    assert exc_info.value.status_code == 307
    assert exc_info.value.headers["Location"] == "/login"


@pytest.mark.asyncio
async def test_get_current_user_from_cookie_invalid_token_redirects(make_request):
    request = make_request("/")
    # Inject a fake cookie by patching .cookies
    request._cookies = {"sb-access-token": "bad.token.here"}  # type: ignore[attr-defined]
    with patch.object(type(request), "cookies", property(lambda self: self._cookies)):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user_from_cookie(request)
    assert exc_info.value.status_code in (307, 401)


@pytest.mark.asyncio
async def test_get_current_user_from_cookie_valid_token(monkeypatch, make_request):
    import shopping_agent.auth as auth_mod
    from shopping_agent.auth import _TOKEN_CACHE
    _TOKEN_CACHE.cache.clear()
    monkeypatch.setattr(auth_mod.settings, "supabase_jwt_secret", _JWT_SECRET)

    token = _make_hs256_token()
    request = make_request("/")
    request._cookies = {"sb-access-token": token}  # type: ignore[attr-defined]
    with patch.object(type(request), "cookies", property(lambda self: self._cookies)):
        user = await get_current_user_from_cookie(request)
    assert isinstance(user, CurrentUser)


# ---------------------------------------------------------------------------
# get_current_user (Bearer)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_current_user_no_credentials_raises():
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(credentials=None)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_valid_bearer(monkeypatch):
    import shopping_agent.auth as auth_mod
    from shopping_agent.auth import _TOKEN_CACHE
    _TOKEN_CACHE.cache.clear()
    monkeypatch.setattr(auth_mod.settings, "supabase_jwt_secret", _JWT_SECRET)

    token = _make_hs256_token()
    creds = MagicMock()
    creds.credentials = token
    user = await get_current_user(credentials=creds)
    assert isinstance(user, CurrentUser)
