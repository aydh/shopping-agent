from datetime import date

from sqlalchemy import Date, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .product import Product


class ConsumptionPrediction(TimestampMixin, Base):
    __tablename__ = "consumption_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), unique=True)
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
