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

from .config import APP_TIMEZONE, PRICE_REFRESH_INTERVAL_HOURS, PRICE_REFRESH_JITTER_MINUTES, settings
from .database import init_db
from .models import Store
from .routes.mcp import mcp
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
    _scheduler.start()
    # First run 60 seconds after startup, then every PRICE_REFRESH_INTERVAL_HOURS ± jitter
    first_run_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    _scheduler.add_job(
        _run_refresh_then_reschedule,
        "date",
        run_date=first_run_at,
        id="price_refresh",
        replace_existing=True,
    )
    logger.info("[Scheduler] Initial price refresh scheduled for %s", first_run_at.astimezone(APP_TIMEZONE).isoformat())
    try:
        yield
    finally:
        _scheduler.shutdown(wait=False)


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
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

from .routes import api_auth, api_cart, api_orders, api_predictions, api_prices, api_shopping_list, views  # noqa: E402

app.include_router(views.router)
app.include_router(api_auth.router, prefix="/api/auth")
app.include_router(api_orders.router, prefix="/api/orders")
app.include_router(api_predictions.router, prefix="/api/predictions")
app.include_router(api_shopping_list.router, prefix="/api/shopping-list")
app.include_router(api_cart.router, prefix="/api/cart")
app.include_router(api_prices.router, prefix="/api/prices")

app.mount("/mcp", mcp_app)
