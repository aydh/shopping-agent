from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    await init_db()
    yield


app = FastAPI(title="Shopping Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

from .routes import api_auth, api_cart, api_orders, api_predictions, api_prices, api_shopping_list, views  # noqa: E402

app.include_router(views.router)
app.include_router(api_auth.router, prefix="/api/auth")
app.include_router(api_orders.router, prefix="/api/orders")
app.include_router(api_predictions.router, prefix="/api/predictions")
app.include_router(api_shopping_list.router, prefix="/api/shopping-list")
app.include_router(api_cart.router, prefix="/api/cart")
app.include_router(api_prices.router, prefix="/api/prices")
