"""Price refresh — streams progress via SSE when backend scheduler is running."""
import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ...auth import CurrentUser, get_current_user_from_cookie
from ...database import async_session
from ...db_helpers import store_from_string
from ...models import PriceRefreshStatus, Store

router = APIRouter()
logger = logging.getLogger(__name__)

# Shared broadcast queues for price refresh progress — only the scheduler writes to these
# SSE clients only read (listen)
refresh_broadcasts: dict[Store, asyncio.Queue[dict[str, Any] | None]] = {
    Store.COLES: asyncio.Queue(),
    Store.WOOLWORTHS: asyncio.Queue(),
}


@router.get("/refresh-stream/{store}")
async def refresh_prices_stream(
    store: str,
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> StreamingResponse:
    """SSE endpoint: streams price refresh progress from the backend scheduler.

    This is READ-ONLY — it only listens to broadcasts from the scheduler.
    It does NOT start any refresh tasks.
    """
    store_enum = store_from_string(store)

    async def generate():
        broadcast_queue = refresh_broadcasts[store_enum]

        try:
            while True:
                # Wait for the next broadcast event (only scheduler puts events here)
                event = await broadcast_queue.get()

                if event is None:
                    # End of refresh signal
                    yield f"event: done\ndata: {json.dumps({'message': 'Refresh complete'})}\n\n"
                    break

                # Broadcast contains event data (progress, done, etc.)
                event_type = event.pop("event_type", "progress")
                yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
        except Exception:
            logger.exception("Unexpected error during price refresh stream for %s", store_enum.value)
            yield f"event: error\ndata: {json.dumps({'message': 'Stream error — see server logs'})}\n\n"

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/status")
async def get_refresh_status(
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> dict[str, Any]:
    """Returns current price refresh status for both stores from PriceRefreshStatus table."""
    async with async_session() as session:
        coles_result = await session.execute(
            select(PriceRefreshStatus).where(PriceRefreshStatus.store == Store.COLES)
        )
        coles_status = coles_result.scalars().first()

        ww_result = await session.execute(
            select(PriceRefreshStatus).where(PriceRefreshStatus.store == Store.WOOLWORTHS)
        )
        ww_status = ww_result.scalars().first()

    def _status_dict(status: PriceRefreshStatus | None) -> dict[str, Any]:
        if not status:
            return {
                "is_running": False,
                "total_products": 0,
                "updated_count": 0,
                "unavailable_count": 0,
                "not_found_count": 0,
                "error_count": 0,
                "last_run_at": None,
                "next_run_at": None,
            }
        return {
            "is_running": status.is_running,
            "total_products": status.total_products,
            "updated_count": status.updated_count,
            "unavailable_count": status.unavailable_count,
            "not_found_count": status.not_found_count,
            "error_count": status.error_count,
            "last_run_at": status.last_run_at.isoformat() if status.last_run_at else None,
            "next_run_at": status.next_run_at.isoformat() if status.next_run_at else None,
        }

    return {
        "coles": _status_dict(coles_status),
        "woolworths": _status_dict(ww_status),
    }
