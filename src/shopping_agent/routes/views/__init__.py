"""View routes package — one module per page domain."""
from fastapi import APIRouter

from .auth_callback import router as auth_callback_router
from .dashboard import router as dashboard_router
from .health import router as health_router
from .login import router as login_router
from .orders import router as orders_router
from .predictions import router as predictions_router
from .prices import router as prices_router
from .product_lookup import router as product_lookup_router
from .register import router as register_router
from .settings import router as settings_router
from .shopping_list import router as shopping_list_router

router = APIRouter()
router.include_router(health_router)
router.include_router(login_router)
router.include_router(register_router)
router.include_router(auth_callback_router)
router.include_router(dashboard_router)
router.include_router(orders_router)
router.include_router(predictions_router)
router.include_router(prices_router)
router.include_router(product_lookup_router)
router.include_router(shopping_list_router)
router.include_router(settings_router)
