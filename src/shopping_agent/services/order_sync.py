import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Order, OrderItem, Product, Store
from ..scrapers.base import ScrapedOrder

logger = logging.getLogger(__name__)


async def sync_orders(
    session: AsyncSession,
    scraped_orders: list[ScrapedOrder],
    store: Store,
) -> int:
    """Upsert scraped orders into the database. Returns count of new orders."""
    new_count = 0

    for scraped in scraped_orders:
        # Check if order already exists
        existing = await session.execute(
            select(Order).where(Order.store_order_id == scraped.store_order_id)
        )
        if existing.scalar_one_or_none():
            continue

        # Create order
        order = Order(
            store=store,
            store_order_id=scraped.store_order_id,
            order_date=scraped.order_date,
            total_amount=scraped.total_amount,
            status=scraped.status,
        )
        session.add(order)
        await session.flush()

        # Upsert products and create order items
        for scraped_item in scraped.items:
            product = await _upsert_product(session, scraped_item, store)
            order_item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=scraped_item.quantity,
                price_paid=scraped_item.price_paid,
            )
            session.add(order_item)

        new_count += 1

    await session.commit()
    logger.info("Synced %d new orders from %s", new_count, store.value)
    return new_count


async def _upsert_product(session: AsyncSession, item, store: Store) -> Product:
    """Find or create a product from a scraped order item."""
    result = await session.execute(
        select(Product).where(
            Product.store == store,
            Product.store_product_id == item.store_product_id,
        )
    )
    product = result.scalar_one_or_none()

    if product:
        # Update with latest info
        product.name = item.name
        if item.brand:
            product.brand = item.brand
        if item.unit_size:
            product.unit_size = item.unit_size
        if item.image_url:
            product.image_url = item.image_url
    else:
        product = Product(
            store=store,
            store_product_id=item.store_product_id,
            name=item.name,
            brand=item.brand,
            unit_size=item.unit_size,
            image_url=item.image_url,
            category=getattr(item, "category", None),
            current_price=item.price_paid,
        )
        session.add(product)
        await session.flush()

    return product
