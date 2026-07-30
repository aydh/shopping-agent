from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from .order import OrderItem
    from .shopping_list import ShoppingListItem

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, Float, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


class Store(enum.Enum):
    COLES = "coles"
    WOOLWORTHS = "woolworths"


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("store", "store_product_id", name="uq_store_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store: Mapped[Store] = mapped_column(SAEnum(Store), index=True)
    store_product_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(512))
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)
    category: Mapped[str | None] = mapped_column(String(256), nullable=True)
    unit_size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price_measure: Mapped[str | None] = mapped_column(String(32), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    product_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    not_found: Mapped[bool] = mapped_column(Boolean, default=False)

    order_items: Mapped[list["OrderItem"]] = relationship(back_populates="product")  # noqa: F821
    shopping_list_items: Mapped[list["ShoppingListItem"]] = relationship(back_populates="product")  # noqa: F821


class UserProductPreferences(TimestampMixin, Base):
    __tablename__ = "user_product_preferences"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_user_product_pref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    exclude_from_predictions: Mapped[bool] = mapped_column(Boolean, default=False)

    product: Mapped["Product"] = relationship()


class ProductMatch(TimestampMixin, Base):
    __tablename__ = "product_matches"
    __table_args__ = (UniqueConstraint("product_a_id", "product_b_id", name="uq_product_match"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_a_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    product_b_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    match_method: Mapped[str] = mapped_column(String(32))
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_rejected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    product_a: Mapped["Product"] = relationship(foreign_keys=[product_a_id])
    product_b: Mapped["Product"] = relationship(foreign_keys=[product_b_id])


class PriceHistory(Base):
    """One row per price regime: the price held from recorded_at until at
    least last_seen_at. A new row is only created when the price changes."""

    __tablename__ = "price_history"
    __table_args__ = (
        Index("ix_price_history_product_id_recorded_at", "product_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    store: Mapped[Store] = mapped_column(SAEnum(Store))
    price: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())

    product: Mapped["Product"] = relationship()
