"""Price API routes package."""
from fastapi import APIRouter

from .charts import router as charts_router
from .matches import router as matches_router
from .products import router as products_router
from .refresh import router as refresh_router
from .search import router as search_router

router = APIRouter()
router.include_router(refresh_router)
router.include_router(matches_router)
router.include_router(search_router)
router.include_router(products_router)
router.include_router(charts_router)
