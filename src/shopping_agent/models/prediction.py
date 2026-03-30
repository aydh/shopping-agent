import uuid
from datetime import date

from sqlalchemy import Date, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .product import Product


class ConsumptionPrediction(TimestampMixin, Base):
    __tablename__ = "consumption_predictions"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_user_product_prediction"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    avg_purchase_interval_days: Mapped[float] = mapped_column(Float)
    avg_quantity_per_purchase: Mapped[float] = mapped_column(Float)
    estimated_daily_consumption: Mapped[float] = mapped_column(Float)
    confidence_score: Mapped[float] = mapped_column(Float)
    last_purchased_date: Mapped[date] = mapped_column(Date)
    predicted_runout_date: Mapped[date] = mapped_column(Date)
    next_purchase_date: Mapped[date] = mapped_column(Date)
    purchase_count: Mapped[int] = mapped_column(Integer)
    last_purchase_quantity: Mapped[int] = mapped_column(Integer, server_default="1")
    last_purchase_store: Mapped[str] = mapped_column(String, server_default="")

    product: Mapped["Product"] = relationship()
