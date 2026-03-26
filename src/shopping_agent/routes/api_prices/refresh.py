"""Price refresh background task and progress polling."""
import logging
from typing import TypedDict

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import get_user_session_from_cookie
from ...db_helpers import store_from_string
from ...models import (
    Product,
    Store,
)
from ...scrapers.registry import coles_scraper as _coles_scraper, woolworths_scraper as _ww_scraper
from ...services.price_refresh import do_price_refresh

router = APIRouter()
logger = logging.getLogger(__name__)


class RefreshState(TypedDict, total=False):
    done: int
    total: int
    running: bool
    updated: int


# In-memory progress tracking: store_value -> {done, total, running}
_refresh_progress: dict[str, RefreshState] = {}


async def _do_price_refresh(store_enum: Store) -> None:
    """Background task wrapper: delegates to service and updates progress."""
    key = store_enum.value
    current_total = _refresh_progress.get(key, {}).get("total", 0)
    _refresh_progress[key] = {"done": 0, "total": current_total, "running": True}
    updated = 0
    total = 0

    async def _update_progress(done: int, total_count: int) -> None:
        _refresh_progress[key] = {"done": done, "total": total_count, "running": True}

    try:
        updated, total = await do_price_refresh(store_enum, progress_callback=_update_progress)
    except Exception:
        logger.exception("[PriceRefresh] Unexpected error during %s refresh", store_enum.value)
    finally:
        _refresh_progress[key] = {"done": total, "total": total, "running": False, "updated": updated}


@router.post("/refresh/{store}")
async def refresh_prices(store: str, background_tasks: BackgroundTasks, user: CurrentUser = Depends(get_current_user_from_cookie),
    session: AsyncSession = Depends(get_user_session_from_cookie)) -> HTMLResponse:
    """Kick off a background price refresh for the given store."""
    store_enum = store_from_string(store)
    store_val = store_enum.value
    scraper = _coles_scraper if store_enum == Store.COLES else _ww_scraper

    if _refresh_progress.get(store_val, {}).get("running"):
        return HTMLResponse(
            f'<span class="text-yellow-600 text-sm">Refresh already running for {store_enum.value.title()}.</span>'
        )

    if not await scraper.is_authenticated():
        return HTMLResponse(f'<span class="text-red-600 text-sm">Not connected to {store_enum.value.title()}.</span>')

    result = await session.execute(select(Product).where(Product.store == store_enum))
    count = len(result.scalars().all())
    if not count:
        return HTMLResponse(f'<span class="text-yellow-600 text-sm">No {store_enum.value.title()} products.</span>')

    _refresh_progress[store_val] = {"done": 0, "total": count, "running": True}
    background_tasks.add_task(_do_price_refresh, store_enum)
    return HTMLResponse(
        f'<span id="refresh-progress-{store_val}" class="text-blue-600 text-sm"'
        f' hx-get="/api/prices/refresh-progress/{store_val}"'
        f' hx-trigger="every {settings.price_refresh_poll_interval_ms}ms"'
        f' hx-target="#refresh-progress-{store_val}"'
        f' hx-swap="outerHTML">0/{count}</span>'
    )


@router.get("/refresh-progress/{store}")
async def refresh_progress(store: str) -> HTMLResponse:
    """Poll endpoint for price refresh progress."""
    state = _refresh_progress.get(store)
    if not state:
        return HTMLResponse("")
    done = state["done"]
    total = state["total"]
    running = state["running"]
    if running:
        return HTMLResponse(
            f'<span id="refresh-progress-{store}" class="text-blue-600 text-sm"'
            f' hx-get="/api/prices/refresh-progress/{store}"'
            f' hx-trigger="every {settings.price_refresh_poll_interval_ms}ms"'
            f' hx-target="#refresh-progress-{store}"'
            f' hx-swap="outerHTML">{done}/{total}</span>'
        )
    updated = state.get("updated", done)
    response = HTMLResponse(
        f'<span class="text-green-600 text-sm">Done — {updated}/{total} updated</span>'
    )
    response.headers["HX-Refresh"] = "true"
    return response
