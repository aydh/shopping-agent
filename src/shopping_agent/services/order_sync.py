import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Order, OrderItem, PriceHistory, Product, Store
from ..scrapers.base import ScrapedOrder, ScrapedOrderItem

logger = logging.getLogger(__name__)


async def sync_orders(
    session: AsyncSession,
    scraped_orders: list[ScrapedOrder],
    store: Store,
    user_id: uuid.UUID,
) -> int:
    """Upsert scraped orders into the database. Returns count of new orders."""
    if not scraped_orders:
        return 0

    # Bulk-fetch existing orders
    store_order_ids = [o.store_order_id for o in scraped_orders]
    existing_orders = {
        o.store_order_id: o
        for o in (await session.execute(
            select(Order).where(
                Order.store_order_id.in_(store_order_ids),
                Order.user_id == user_id,
            )
        )).scalars().all()
    }

    # Bulk-fetch existing products for this store
    all_product_ids = {
        item.store_product_id
        for order in scraped_orders
        for item in order.items
    }
    existing_products: dict[str, Product] = {
        p.store_product_id: p
        for p in (await session.execute(
            select(Product).where(
                Product.store == store,
                Product.store_product_id.in_(all_product_ids),
            )
        )).scalars().all()
    }

    new_count = 0
    # Accumulate new products, order items, and price history entries to flush in bulk
    new_products: list[Product] = []
    new_order_items: list[OrderItem] = []
    # (product_id_placeholder, store_product_id, store, price, recorded_at)
    pending_price_history: list[tuple[str, Store, float, datetime]] = []

    for scraped in scraped_orders:
        existing_order = existing_orders.get(scraped.store_order_id)
        if existing_order:
            if scraped.store_name and not existing_order.store_name:
                existing_order.store_name = scraped.store_name
            if scraped.store_id and not existing_order.store_id:
                existing_order.store_id = scraped.store_id
            continue

        order = Order(
            user_id=user_id,
            store=store,
            store_order_id=scraped.store_order_id,
            order_date=scraped.order_date,
            total_amount=scraped.total_amount,
            status=scraped.status,
            store_name=scraped.store_name,
            store_id=scraped.store_id,
        )
        session.add(order)
        new_count += 1

        for scraped_item in scraped.items:
            product = existing_products.get(scraped_item.store_product_id)
            if product:
                product.name = scraped_item.name
                if scraped_item.brand:
                    product.brand = scraped_item.brand
                if scraped_item.unit_size:
                    product.unit_size = scraped_item.unit_size
                if scraped_item.image_url:
                    product.image_url = scraped_item.image_url
            else:
                product = Product(
                    store=store,
                    store_product_id=scraped_item.store_product_id,
                    name=scraped_item.name,
                    brand=scraped_item.brand,
                    unit_size=scraped_item.unit_size,
                    image_url=scraped_item.image_url,
                    category=getattr(scraped_item, "category", None),
                    current_price=scraped_item.price_paid,
                )
                session.add(product)
                existing_products[scraped_item.store_product_id] = product
                new_products.append(product)

            if scraped_item.price_paid:
                recorded_at = datetime(scraped.order_date.year, scraped.order_date.month, scraped.order_date.day, tzinfo=timezone.utc)
                pending_price_history.append((
                    scraped_item.store_product_id, store, scraped_item.price_paid, recorded_at
                ))

            new_order_items.append((order, scraped_item, scraped_item.store_product_id))

    # Flush to get IDs for new products and orders
    await session.flush()

    # Resolve product IDs for order items now that flush has assigned them
    for order, scraped_item, spid in new_order_items:
        product = existing_products[spid]
        session.add(OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=scraped_item.quantity,
            price_paid=scraped_item.price_paid,
        ))

    # Bulk-check which price history entries already exist, then insert missing ones
    if pending_price_history:
        ph_product_ids = list({existing_products[spid].id for spid, *_ in pending_price_history})
        ph_dates = list({recorded_at for _, _, _, recorded_at in pending_price_history})
        existing_ph = {
            (ph.product_id, ph.recorded_at)
            for ph in (await session.execute(
                select(PriceHistory).where(
                    PriceHistory.product_id.in_(ph_product_ids),
                    PriceHistory.recorded_at.in_(ph_dates),
                )
            )).scalars().all()
        }
        for spid, ph_store, price, recorded_at in pending_price_history:
            product = existing_products[spid]
            if (product.id, recorded_at) not in existing_ph:
                session.add(PriceHistory(
                    product_id=product.id,
                    store=ph_store,
                    price=price,
                    recorded_at=recorded_at,
                ))
                existing_ph.add((product.id, recorded_at))  # prevent duplicates within same sync

    await session.commit()
    logger.info("Synced %d new orders from %s", new_count, store.value)
    return new_count
