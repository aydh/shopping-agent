"""Shared SQLAlchemy query helpers and store enum utilities."""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import ScalarSelect, Select, select

from .models.product import Product, Store, UserProductPreferences


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


def hidden_product_ids_subquery(user_id: UUID) -> ScalarSelect:
    """Scalar subquery returning product IDs hidden by the given user."""
    return (
        select(UserProductPreferences.product_id)
        .where(
            UserProductPreferences.user_id == user_id,
            UserProductPreferences.is_hidden.is_(True),
        )
        .scalar_subquery()
    )


def visible_products_query(user_id: UUID) -> Select:
    """Return a base SELECT for products that are not hidden by the given user.

    Args:
        user_id: The current user's UUID; only their hide preferences are checked.

    Returns:
        SQLAlchemy Select statement filtered to products not hidden by this user.
    """
    return select(Product).where(Product.id.notin_(hidden_product_ids_subquery(user_id)))

