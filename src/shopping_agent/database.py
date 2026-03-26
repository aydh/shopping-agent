import json
from typing import AsyncGenerator
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from fastapi import Depends

from .config import settings
from .auth import CurrentUser, get_current_user, get_current_user_from_cookie

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Plain session — no RLS claims. Use for background tasks and scheduler jobs."""
    async with async_session() as session:
        yield session


async def set_rls_claims(session: AsyncSession, user_id: UUID) -> None:
    """Inject Supabase JWT claims into the current transaction so RLS auth.uid() works."""
    claims_json = json.dumps({"sub": str(user_id), "role": "authenticated"})
    # Postgres "SET <guc> = <value>" does not reliably accept bind parameters for
    # the RHS (it can produce syntax errors like "at or near $1").
    # We inline a properly-escaped JSON literal instead.
    claims_literal = claims_json.replace("'", "''")
    # Supabase expects this parameter as a JSON string for its JWT claims logic.
    # Don't cast here: in some setups, Postgres may not accept `::jsonb` on this
    # GUC assignment.
    await session.execute(text(f"SET LOCAL request.jwt.claims = '{claims_literal}'"))
    await session.execute(text("SET LOCAL ROLE authenticated"))


async def get_user_session(
    user: CurrentUser = Depends(get_current_user),
) -> AsyncGenerator[AsyncSession, None]:
    """RLS-injecting session — for authenticated API routes (Bearer header)."""
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user.user_id)
            yield session


async def get_user_session_from_cookie(
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> AsyncGenerator[AsyncSession, None]:
    """RLS-injecting session — for authenticated HTML page routes (cookie)."""
    async with async_session() as session:
        async with session.begin():
            await set_rls_claims(session, user.user_id)
            yield session


async def verify_db_connection() -> None:
    """Run a minimal query to confirm the database is reachable."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def init_db() -> None:
    """Verify the database connection is healthy at startup."""
    await verify_db_connection()
