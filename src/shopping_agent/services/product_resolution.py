"""Shared helper for resolving ProductMatch partner products."""
import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..log_utils import scrub
from ..models import Product, ProductMatch

logger = logging.getLogger(__name__)


async def get_partner_product(
    session: AsyncSession,
    product_id: int,
    target_store: str,
) -> Product | None:
    """Return the partner Product for the given product_id across a ProductMatch.

    Looks up the non-rejected ProductMatch where the given product_id appears
    on either side, then fetches and returns the other (partner) product.

    Args:
        session: The async SQLAlchemy session.
        product_id: The ID of the known product.
        target_store: Informational — the caller's intended store; not used to
            filter the query, but logged for debugging. Pass the store string
            ("coles" or "woolworths") for traceability.

    Returns:
        The partner Product, or None if no match exists or the partner row is
        missing from the database.
    """
    result = await session.execute(
        select(ProductMatch).where(
            or_(
                ProductMatch.product_a_id == product_id,
                ProductMatch.product_b_id == product_id,
            ),
            ProductMatch.is_rejected == False,  # noqa: E712
        )
    )
    match = result.scalars().first()
    if not match:
        logger.debug(
            "get_partner_product: no match found for product_id=%s (target_store=%s)",
            product_id,
            scrub(target_store),
        )
        return None

    partner_id = (
        match.product_b_id
        if match.product_a_id == product_id
        else match.product_a_id
    )
    partner = await session.get(Product, partner_id)
    if not partner:
        logger.warning(
            "get_partner_product: match %s references missing partner product_id=%s",
            match.id,
            partner_id,
        )
    return partner
