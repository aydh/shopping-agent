import logging
import time
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwk, jwt

from .config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
_JWKS_CACHE_TTL_S = 3600
_JWKS_CACHE: dict[str, tuple[float, dict]] = {}
_TOKEN_CACHE_TTL_S = 300
_TOKEN_CACHE: dict[str, tuple[float, dict]] = {}


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
        cached = _TOKEN_CACHE.get(token)
        now = time.time()
        if cached and (now - cached[0]) < _TOKEN_CACHE_TTL_S:
            return cached[1]

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
            _TOKEN_CACHE[token] = (now, claims)
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
                jwks_cached = _JWKS_CACHE.get(cache_key)
                if jwks_cached and (now - jwks_cached[0]) < _JWKS_CACHE_TTL_S:
                    jwks = jwks_cached[1]
                else:
                    keys_url = f"{settings.supabase_url}/auth/v1/keys"
                    with httpx.Client(timeout=5.0) as client:
                        resp = client.get(keys_url)
                        resp.raise_for_status()
                        jwks = resp.json()
                    _JWKS_CACHE[cache_key] = (now, jwks)

                keys = jwks.get("keys", [])
                jwk_key = next((k for k in keys if k.get("kid") == kid), None) if kid else None
                if jwk_key is None and keys:
                    # Fallback: if kid is missing/unmatched, try the first key.
                    jwk_key = keys[0]
                if jwk_key is None:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="No matching JWT key found",
                        headers={"WWW-Authenticate": "Bearer"},
                    )

                public_key = jwk.construct(jwk_key)
                issuer = f"{settings.supabase_url}/auth/v1"
                claims = jwt.decode(
                    token,
                    public_key,
                    algorithms=[alg],
                    audience="authenticated",
                    issuer=issuer,
                )
                _TOKEN_CACHE[token] = (now, claims)
                return claims
            except Exception:
                logger.debug("JWKS verification failed; falling back to /auth/v1/user validation")

                user_url = f"{settings.supabase_url}/auth/v1/user"
                headers = {
                    "apikey": settings.supabase_anon_key,
                    "Authorization": f"Bearer {token}",
                }
                with httpx.Client(timeout=5.0) as client:
                    resp = client.get(user_url, headers=headers)
                    resp.raise_for_status()
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
                _TOKEN_CACHE[token] = (now, claims)
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
