import enum
import uuid
from datetime import date

from sqlalchemy import Boolean, Date, Enum as SAEnum, Float, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .product import Product, Store


class ListStatus(enum.Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    ORDERED = "ordered"


class ShoppingList(TimestampMixin, Base):
    __tablename__ = "shopping_lists"

    user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    target_date: Mapped[date] = mapped_column(Date)
    status: Mapped[ListStatus] = mapped_column(SAEnum(ListStatus), default=ListStatus.DRAFT)
    preferred_store: Mapped[Store | None] = mapped_column(SAEnum(Store), nullable=True)
    estimated_total: Mapped[float | None] = mapped_column(Float, nullable=True)

    items: Mapped[list["ShoppingListItem"]] = relationship(
        back_populates="shopping_list", cascade="all, delete-orphan",
        order_by="ShoppingListItem.created_at",
    )


class ShoppingListItem(TimestampMixin, Base):
    __tablename__ = "shopping_list_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    shopping_list_id: Mapped[int] = mapped_column(ForeignKey("shopping_lists.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    coles_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    woolworths_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    chosen_store: Mapped[Store | None] = mapped_column(SAEnum(Store), nullable=True)
    is_user_added: Mapped[bool] = mapped_column(Boolean, default=False)
    is_removed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_ordered: Mapped[bool] = mapped_column(Boolean, default=False)

    shopping_list: Mapped["ShoppingList"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(back_populates="shopping_list_items")

    __table_args__ = (
        Index(
            "uq_sli_active_product",
            "shopping_list_id",
            "product_id",
            unique=True,
            postgresql_where=text("is_removed = false"),
        ),
    )
