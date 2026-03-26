import json
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from .config import settings

_bearer = HTTPBearer(auto_error=False)


class CurrentUser:
    def __init__(self, user_id: UUID, email: str | None, raw_claims: dict) -> None:
        self.user_id = user_id
        self.email = email
        self.raw_claims = raw_claims


def _decode_token(token: str) -> dict:
    """Decode and verify a Supabase JWT. Raises HTTPException on failure."""
    if not settings.supabase_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth not configured",
        )
    try:
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
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
    """Verify from sb-access-token cookie — for HTML page routes."""
    token = request.cookies.get("sb-access-token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    claims = _decode_token(token)
    return _claims_to_user(claims)
