import enum

from sqlalchemy import Boolean, Enum as SAEnum, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


class Store(enum.Enum):
    COLES = "coles"
    WOOLWORTHS = "woolworths"


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("store", "store_product_id", name="uq_store_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store: Mapped[Store] = mapped_column(SAEnum(Store))
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

    order_items: Mapped[list["OrderItem"]] = relationship(back_populates="product")  # noqa: F821


class ProductMatch(TimestampMixin, Base):
    __tablename__ = "product_matches"
    __table_args__ = (UniqueConstraint("product_a_id", "product_b_id", name="uq_product_match"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_a_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    product_b_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    confidence: Mapped[float] = mapped_column(Float)
    match_method: Mapped[str] = mapped_column(String(32))
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    product_a: Mapped["Product"] = relationship(foreign_keys=[product_a_id])
    product_b: Mapped["Product"] = relationship(foreign_keys=[product_b_id])
