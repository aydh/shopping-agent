import uuid

from sqlalchemy import Enum as SAEnum, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin
from .product import Store


class StoreCookies(TimestampMixin, Base):
    """Persists session cookies for a store in the database."""

    __tablename__ = "store_cookies"
    __table_args__ = (UniqueConstraint("user_id", "store", name="uq_user_store_cookies"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    store: Mapped[Store] = mapped_column(SAEnum(Store), index=True)
    cookies_json: Mapped[str] = mapped_column(Text)  # JSON array of cookie dicts
