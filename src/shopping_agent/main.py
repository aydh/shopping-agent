import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastmcp.utilities.lifespan import combine_lifespans

from .config import settings
from .database import init_db
from .routes.mcp import mcp

BASE_DIR = Path(__file__).resolve().parent


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


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Shopping Agent",
    lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
)
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
