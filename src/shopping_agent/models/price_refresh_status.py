from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .product import Store


class PriceRefreshStatus(Base):
    """Tracks the real-time status of price refresh runs, one row per store."""

    __tablename__ = "price_refresh_status"
    __table_args__ = (UniqueConstraint("store", name="uq_price_refresh_status_store"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store: Mapped[Store] = mapped_column(SAEnum(Store), index=True)

    # Running state
    is_running: Mapped[bool] = mapped_column(Boolean, default=False)

    # Counts for current or last run
    total_products: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    unavailable_count: Mapped[int] = mapped_column(Integer, default=0)
    not_found_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)

    # Timing
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
