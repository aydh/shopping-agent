import uuid
from datetime import date

from sqlalchemy import Date, Enum as SAEnum, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .product import Product, Store


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("user_id", "store_order_id", name="uq_user_store_order"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    store: Mapped[Store] = mapped_column(SAEnum(Store), index=True)
    store_order_id: Mapped[str] = mapped_column(String(64))
    order_date: Mapped[date] = mapped_column(Date)
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    store_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    store_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(TimestampMixin, Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    price_paid: Mapped[float] = mapped_column(Float)
    was_substituted: Mapped[bool] = mapped_column(default=False)

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(back_populates="order_items")
