"""Shopping list route package."""
from fastapi import APIRouter

from .candidates import router as candidates_router
from .crud import router as crud_router
from .items import router as items_router
from .stores import router as stores_router

router = APIRouter()
router.include_router(crud_router)
router.include_router(items_router)
router.include_router(stores_router)
router.include_router(candidates_router)

__all__ = ["router"]
