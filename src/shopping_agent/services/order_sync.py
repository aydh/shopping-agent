import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Order, OrderItem, PriceHistory, Product, Store
from ..scrapers.base import ScrapedOrder, ScrapedOrderItem

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
        existing_order = existing.scalar_one_or_none()
        if existing_order:
            # Backfill store_name/store_id if now available
            if scraped.store_name and not existing_order.store_name:
                existing_order.store_name = scraped.store_name
            if scraped.store_id and not existing_order.store_id:
                existing_order.store_id = scraped.store_id
            continue

        # Create order
        order = Order(
            store=store,
            store_order_id=scraped.store_order_id,
            order_date=scraped.order_date,
            total_amount=scraped.total_amount,
            status=scraped.status,
            store_name=scraped.store_name,
            store_id=scraped.store_id,
        )
        session.add(order)
        await session.flush()

        # Upsert products and create order items
        for scraped_item in scraped.items:
            product = await _upsert_product(session, scraped_item, store, scraped.order_date)
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


async def _upsert_product(session: AsyncSession, item: ScrapedOrderItem, store: Store, order_date: date) -> Product:
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

    # Record price history using the order date — one entry per product per day
    if item.price_paid:
        recorded_at = datetime.combine(order_date, datetime.min.time())
        existing_ph = await session.execute(
            select(PriceHistory).where(
                PriceHistory.product_id == product.id,
                PriceHistory.recorded_at == recorded_at,
            )
        )
        if not existing_ph.scalar_one_or_none():
            session.add(PriceHistory(
                product_id=product.id,
                store=store,
                price=item.price_paid,
                recorded_at=recorded_at,
            ))

    return product
