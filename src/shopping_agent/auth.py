import logging
import time
from collections import OrderedDict
from typing import Any, Generic, TypeVar
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwk, jwt  # type: ignore[import-untyped]

from .config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


_VT = TypeVar("_VT")


class _TTLCache(Generic[_VT]):
    """Simple bounded TTL cache with automatic cleanup of expired entries."""

    def __init__(self, ttl_seconds: int, max_size: int = 1000):
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self.cache: OrderedDict[str, tuple[float, _VT]] = OrderedDict()

    def get(self, key: str) -> _VT | None:
        """Get value if it exists and hasn't expired; otherwise return None."""
        if key not in self.cache:
            return None
        timestamp, value = self.cache[key]
        if time.time() - timestamp >= self.ttl_seconds:
            # Entry expired; remove it
            del self.cache[key]
            return None
        return value

    def set(self, key: str, value: _VT) -> None:
        """Set a value in the cache. Removes oldest entry if cache is full."""
        # Remove key if already exists (to maintain LRU ordering)
        if key in self.cache:
            del self.cache[key]
        # If cache is full, remove oldest entry (first in OrderedDict)
        if len(self.cache) >= self.max_size:
            oldest_key = next(iter(self.cache))
            del self.cache[oldest_key]
        # Add new entry
        self.cache[key] = (time.time(), value)


_JWKS_CACHE_TTL_S = 3600
_JWKS_CACHE: _TTLCache[dict[str, Any]] = _TTLCache(ttl_seconds=_JWKS_CACHE_TTL_S, max_size=10)
_TOKEN_CACHE_TTL_S = 300
_TOKEN_CACHE: _TTLCache[dict[str, Any]] = _TTLCache(ttl_seconds=_TOKEN_CACHE_TTL_S, max_size=10000)


class CurrentUser:
    def __init__(self, user_id: UUID, email: str | None, raw_claims: dict) -> None:
        self.user_id = user_id
        self.email = email
        self.raw_claims = raw_claims


def _decode_token(token: str) -> dict:
    """Decode and verify a Supabase JWT.

    Supabase deployments may sign tokens with HS256 (shared secret) or with
    RS256/ES256 (JWKS). This verifies based on the token `alg`.
    """
    try:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        kid = header.get("kid")
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token header: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        cached_claims = _TOKEN_CACHE.get(token)
        if cached_claims:
            return cached_claims

        if alg == "HS256":
            if not settings.supabase_jwt_secret:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Auth not configured",
                )
            claims = jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
            )
            _TOKEN_CACHE.set(token, claims)
            return claims

        if alg in ("RS256", "ES256"):
            if not settings.supabase_url:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Supabase URL not configured",
                )
            if not settings.supabase_anon_key:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Supabase anon key not configured",
                )

            # Prefer local signature verification via JWKS, but fall back to
            # Supabase's `/auth/v1/user` endpoint because some Supabase
            # configurations block JWKS access.
            try:
                cache_key = settings.supabase_url
                jwks = _JWKS_CACHE.get(cache_key)
                if jwks is None:
                    keys_url = f"{settings.supabase_url}/auth/v1/.well-known/jwks.json"
                    with httpx.Client(timeout=5.0) as client:
                        resp = client.get(
                            keys_url,
                            headers={"apikey": settings.supabase_anon_key or ""},
                        )
                    if resp.status_code == 200:
                        jwks = resp.json()
                    else:
                        # Endpoint unavailable (e.g. 404) — cache empty so we
                        # don't retry on every request; fallback path handles auth.
                        logger.warning(
                            "JWKS endpoint returned %d; will use /auth/v1/user fallback",
                            resp.status_code,
                        )
                        jwks = {}
                    _JWKS_CACHE.set(cache_key, jwks)

                keys = jwks.get("keys", [])
                jwk_key = next((k for k in keys if k.get("kid") == kid), None) if kid else None
                if jwk_key is None and keys:
                    # Fallback: if kid is missing/unmatched, try the first key.
                    if kid:
                        logger.warning(
                            "JWKS: no key matched kid=%r; falling back to first key — "
                            "check that your JWKS is up-to-date",
                            kid,
                        )
                    jwk_key = keys[0]
                if jwk_key is None:
                    raise ValueError("No matching JWT key found in JWKS; falling back to /auth/v1/user")

                public_key = jwk.construct(jwk_key)
                issuer = f"{settings.supabase_url}/auth/v1"
                claims = jwt.decode(
                    token,
                    public_key,
                    algorithms=[alg],
                    audience="authenticated",
                    issuer=issuer,
                )
                _TOKEN_CACHE.set(token, claims)
                return claims
            except HTTPException:
                raise
            except ExpiredSignatureError:
                # Token is definitively expired — no point calling Supabase.
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token has expired",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            except Exception as exc:
                logger.warning(
                    "JWKS verification failed (%s: %s); falling back to /auth/v1/user validation",
                    type(exc).__name__,
                    exc,
                )

                user_url = f"{settings.supabase_url}/auth/v1/user"
                headers = {
                    "apikey": settings.supabase_anon_key,
                    "Authorization": f"Bearer {token}",
                }
                with httpx.Client(timeout=5.0) as client:
                    resp = client.get(user_url, headers=headers)
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail=f"Token rejected by Supabase ({resp.status_code})",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                user = resp.json()

                claims = {
                    "sub": user.get("id"),
                    "email": user.get("email"),
                    "aud": user.get("aud"),
                    "role": user.get("role"),
                    "raw": user,
                }
                if not claims["sub"]:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Token did not return a user id",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                _TOKEN_CACHE.set(token, claims)
                return claims

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unsupported JWT alg: {alg}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except HTTPException:
        raise
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _claims_to_user(claims: dict) -> CurrentUser:
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing sub claim",
        )
    return CurrentUser(user_id=UUID(sub), email=claims.get("email"), raw_claims=claims)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    """Verify Bearer token — for API/SSE routes."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = _decode_token(credentials.credentials)
    return _claims_to_user(claims)


async def get_current_user_from_cookie(request: Request) -> CurrentUser:
    """Verify from sb-access-token cookie — for HTML page routes.

    Raises a 307 redirect to /login when unauthenticated so HTMX pages
    land on the login screen rather than returning a bare 401.
    """
    token = request.cookies.get("sb-access-token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    try:
        claims = _decode_token(token)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    return _claims_to_user(claims)
