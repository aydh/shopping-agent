"""Health check endpoint for platform probes."""
import logging

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from ...database import verify_db_connection

router = APIRouter()
logger = logging.getLogger(__name__)


@router.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
async def health_check() -> JSONResponse:
    """Return service health for Render and other infrastructure probes."""
    try:
        await verify_db_connection()
    except Exception:  # pragma: no cover - logged and surfaced as a 503
        logger.exception("Health check failed")
        return JSONResponse(
            {"status": "unhealthy"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return JSONResponse({"status": "ok"})
