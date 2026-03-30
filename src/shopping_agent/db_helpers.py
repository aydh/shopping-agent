"""Shared SQLAlchemy query helpers and store enum utilities."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import Select, select

from .models.product import Product, Store


def store_from_string(value: str) -> Store:
    """Convert a string to a Store enum, case-insensitively.

    Args:
        value: Store name string (e.g. "coles", "WOOLWORTHS").

    Returns:
        The matching Store enum value.

    Raises:
        HTTPException(422): If the string does not match any Store.
    """
    try:
        return Store[value.upper()]
    except KeyError:
        valid = [s.value for s in Store]
        raise HTTPException(status_code=422, detail=f"Unknown store '{value}'. Valid values: {valid}")


def visible_products_query() -> Select:
    """Return a base SELECT for products that are not hidden.

    Returns:
        SQLAlchemy Select statement filtered to non-hidden products.
    """
    return select(Product).where(Product.is_hidden.is_(False))
