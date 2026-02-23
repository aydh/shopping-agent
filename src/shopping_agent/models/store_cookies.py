from sqlalchemy import Enum as SAEnum, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin
from .product import Store


class StoreCookies(TimestampMixin, Base):
    """Persists session cookies for a store in the database."""

    __tablename__ = "store_cookies"

    id: Mapped[int] = mapped_column(primary_key=True)
    store: Mapped[Store] = mapped_column(SAEnum(Store), unique=True, index=True)
    cookies_json: Mapped[str] = mapped_column(Text)  # JSON array of cookie dicts
