import json
import logging
import logging.handlers
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastmcp.utilities.lifespan import combine_lifespans

from .auth import _claims_to_user, _decode_token
from .config import APP_TIMEZONE, PRICE_REFRESH_INTERVAL_HOURS, PRICE_REFRESH_JITTER_MINUTES, settings
from .database import init_db
from .models import Store
from .routes.mcp import _mcp_user_id_var, mcp
from .services.price_refresh import do_price_refresh

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    settings.ensure_dirs()

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")

    # Rotating file handler: 10 MB per file, keep 5 backups
    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_dir / "shopping_agent.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("shopping_agent").setLevel(logging.DEBUG)

    # Suppress noisy third-party loggers
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


_configure_logging()

mcp_app = mcp.http_app(path="/")


_scheduler = AsyncIOScheduler()


async def _scheduled_price_refresh() -> None:
    """Run a full price refresh for both stores (all products, no filters)."""
    for store in (Store.COLES, Store.WOOLWORTHS):
        try:
            updated, total = await do_price_refresh(store, all_products=True)
            logger.info("[Scheduler] %s price refresh complete: %d/%d updated", store.value, updated, total)
        except Exception:
            logger.exception("[Scheduler] Price refresh failed for %s", store.value)


def _schedule_next_refresh() -> None:
    """Schedule the next price refresh with random jitter applied."""
    jitter_s = random.uniform(
        -PRICE_REFRESH_JITTER_MINUTES * 60,
        PRICE_REFRESH_JITTER_MINUTES * 60,
    )
    run_at = datetime.now(timezone.utc) + timedelta(hours=PRICE_REFRESH_INTERVAL_HOURS, seconds=jitter_s)
    _scheduler.add_job(
        _run_refresh_then_reschedule,
        "date",
        run_date=run_at,
        id="price_refresh",
        replace_existing=True,
    )
    logger.info("[Scheduler] Next price refresh scheduled for %s", run_at.astimezone(APP_TIMEZONE).isoformat())


async def _run_refresh_then_reschedule() -> None:
    await _scheduled_price_refresh()
    _schedule_next_refresh()


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    await init_db()
    if settings.enable_scheduler:
        _scheduler.start()
        first_run_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        _scheduler.add_job(
            _run_refresh_then_reschedule,
            "date",
            run_date=first_run_at,
            id="price_refresh",
            replace_existing=True,
        )
        logger.info("[Scheduler] Initial price refresh scheduled for %s", first_run_at.astimezone(APP_TIMEZONE).isoformat())
    else:
        logger.info("[Scheduler] Disabled (ENABLE_SCHEDULER=false)")
    try:
        yield
    finally:
        if settings.enable_scheduler:
            _scheduler.shutdown(wait=False)


async def _mcp_401(send, resource_metadata_url: str, error: str | None = None) -> None:
    """Send a 401 Unauthorized ASGI response with a WWW-Authenticate header."""
    www_auth = f'Bearer resource_metadata="{resource_metadata_url}"'
    if error:
        www_auth += f', error="{error}"'
    body = json.dumps({"error": error or "unauthorized"}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            [b"content-type", b"application/json"],
            [b"www-authenticate", www_auth.encode()],
            [b"content-length", str(len(body)).encode()],
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


class MCPAuthMiddleware:
    """Validate Bearer tokens for /mcp/* requests per the MCP OAuth spec.

    On an unauthenticated or invalid request, returns HTTP 401 with a
    WWW-Authenticate header pointing to the Protected Resource Metadata
    document (RFC 9728), which MCP clients use to discover the authorization
    server and start the OAuth 2.1 + PKCE flow.

    On a valid request, stores the authenticated user UUID in _mcp_user_id_var
    so MCP tool handlers can retrieve it via _get_mcp_user_id().

    Non-/mcp paths are passed through unchanged.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode()
        resource_metadata_url = f"{settings.base_url}/.well-known/oauth-protected-resource"

        if not auth.startswith("Bearer "):
            await _mcp_401(send, resource_metadata_url)
            return

        token = auth[len("Bearer "):]
        try:
            claims = _decode_token(token)
            user = _claims_to_user(claims)
        except Exception:
            await _mcp_401(send, resource_metadata_url, error="invalid_token")
            return

        ctx_token = _mcp_user_id_var.set(user.user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _mcp_user_id_var.reset(ctx_token)


class _MCPPathMiddleware:
    """Rewrite /mcp (no trailing slash) to /mcp/ so MCP clients don't get a 307.

    Uses pure ASGI (no BaseHTTPMiddleware) to avoid buffering SSE streams.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


app = FastAPI(
    title="Shopping Agent",
    lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
)
app.add_middleware(_MCPPathMiddleware)
app.add_middleware(MCPAuthMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

from .routes import api_auth, api_cart, api_orders, api_predictions, api_prices, api_shopping_list, views  # noqa: E402

app.include_router(views.router)
app.include_router(api_auth.router, prefix="/api/auth")
app.include_router(api_orders.router, prefix="/api/orders")
app.include_router(api_predictions.router, prefix="/api/predictions")
app.include_router(api_shopping_list.router, prefix="/api/shopping-list")
app.include_router(api_cart.router, prefix="/api/cart")
app.include_router(api_prices.router, prefix="/api/prices")

@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata():
    """Protected Resource Metadata document (RFC 9728).

    MCP clients fetch this after receiving a 401 from /mcp to discover
    the authorization server and start the OAuth 2.1 + PKCE flow.
    """
    return {
        "resource": f"{settings.base_url}/mcp",
        "authorization_servers": [settings.supabase_url],
        "bearer_methods_supported": ["header"],
    }


app.mount("/mcp", mcp_app)
