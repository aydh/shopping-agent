"""Shared SQLAlchemy query helpers and store enum utilities."""
from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models.product import Product, Store


def store_from_string(value: str) -> Store:
    """Convert a string to a Store enum, case-insensitively.

    Args:
        value: Store name string (e.g. "coles", "WOOLWORTHS").

    Returns:
        The matching Store enum value.

    Raises:
        ValueError: If the string does not match any Store.
    """
    try:
        return Store[value.upper()]
    except KeyError:
        valid = [s.value for s in Store]
        raise ValueError(f"Unknown store '{value}'. Valid values: {valid}")


def visible_products_query() -> Select:
    """Return a base SELECT for products that are not hidden.

    Returns:
        SQLAlchemy Select statement filtered to non-hidden products.
    """
    return select(Product).where(Product.is_hidden.is_(False))


async def get_visible_products(session: AsyncSession) -> list[Product]:
    """Fetch all non-hidden products.

    Args:
        session: Active async database session.

    Returns:
        List of visible Product instances.
    """
    result = await session.execute(visible_products_query())
    return list(result.scalars().all())
