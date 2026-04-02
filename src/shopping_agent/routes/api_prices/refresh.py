"""Price refresh — streams progress via SSE as products are updated."""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ...auth import CurrentUser, get_current_user_from_cookie
from ...db_helpers import store_from_string
from ...models import Store
from ...scrapers.registry import coles_scraper as _coles_scraper, woolworths_scraper as _ww_scraper
from ...services.price_refresh import do_price_refresh

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/refresh-stream/{store}")
async def refresh_prices_stream(
    store: str,
    user: CurrentUser = Depends(get_current_user_from_cookie),
) -> StreamingResponse:
    """SSE endpoint: refreshes product prices and streams progress to the browser."""
    store_enum = store_from_string(store)
    scraper = _coles_scraper if store_enum == Store.COLES else _ww_scraper

    async def generate():
        if not await scraper.is_authenticated():
            yield f"event: error\ndata: {json.dumps({'message': f'Not connected to {store_enum.value.title()}'})}\n\n"
            return

        queue: asyncio.Queue[tuple[int, int] | None] = asyncio.Queue()

        async def progress_callback(done: int, total: int) -> None:
            await queue.put((done, total))

        async def run_and_sentinel() -> tuple[int, int]:
            result = await do_price_refresh(store_enum, progress_callback)
            await queue.put(None)  # signal completion
            return result

        task = asyncio.create_task(run_and_sentinel())

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                done, total = item
                yield f"event: progress\ndata: {json.dumps({'done': done, 'total': total})}\n\n"

            updated, total = await task
            yield f"event: done\ndata: {json.dumps({'updated': updated, 'total': total})}\n\n"
        except Exception as e:
            logger.exception("Unexpected error during price refresh stream for %s", store_enum.value)
            yield f"event: error\ndata: {json.dumps({'message': 'Price refresh failed — see server logs'})}\n\n"

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
