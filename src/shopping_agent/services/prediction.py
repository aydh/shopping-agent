import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, stdev

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import ConsumptionPrediction, Order, OrderItem, Product, ProductMatch

logger = logging.getLogger(__name__)


@dataclass
class PurchaseRecord:
    order_date: date
    quantity: int


@dataclass
class ShoppingListCandidate:
    product_id: int
    quantity: int
    reason: str


def compute_prediction(
    purchases: list[PurchaseRecord],
    today: date | None = None,
    decay_factor: float = 0.15,
    lead_time_days: int = 2,
) -> dict | None:
    """
    Compute consumption prediction for a single product.

    Returns dict with prediction fields, or None if insufficient data.
    """
    if len(purchases) < 2:
        return None

    today = today or date.today()
    purchases = sorted(purchases, key=lambda p: p.order_date)

    # Calculate inter-purchase intervals normalized by quantity
    intervals = []
    for i in range(1, len(purchases)):
        days_between = (purchases[i].order_date - purchases[i - 1].order_date).days
        if days_between <= 0:
            continue
        qty = purchases[i - 1].quantity
        days_per_unit = days_between / max(qty, 1)
        intervals.append(
            {
                "days_between": days_between,
                "quantity": qty,
                "days_per_unit": days_per_unit,
            }
        )

    if not intervals:
        return None

    # Weighted average consumption (recent purchases weighted more)
    n = len(intervals)
    total_weight = 0.0
    weighted_sum = 0.0

    for i, interval in enumerate(intervals):
        recency = i / max(n - 1, 1)
        weight = math.exp(decay_factor * (recency - 1) * n)
        daily_consumption = 1.0 / interval["days_per_unit"]
        weighted_sum += daily_consumption * weight
        total_weight += weight

    daily_consumption = weighted_sum / total_weight if total_weight > 0 else 0

    if daily_consumption <= 0:
        return None

    # Average purchase interval
    raw_intervals = [iv["days_between"] for iv in intervals]
    avg_interval = mean(raw_intervals)

    # Average quantity
    all_quantities = [p.quantity for p in purchases]
    avg_quantity = mean(all_quantities)

    # Confidence score
    interval_cv = (stdev(raw_intervals) / avg_interval) if len(raw_intervals) > 1 and avg_interval > 0 else 1.0
    data_confidence = 1 - math.exp(-0.3 * (len(purchases) - 1))
    regularity_confidence = max(0, 1 - interval_cv)
    confidence = round(data_confidence * regularity_confidence, 3)

    # Predict runout
    last_purchase = purchases[-1]
    days_of_supply = last_purchase.quantity / daily_consumption
    runout_date = last_purchase.order_date + timedelta(days=int(days_of_supply))
    next_purchase = runout_date - timedelta(days=lead_time_days)

    return {
        "avg_purchase_interval_days": round(avg_interval, 1),
        "avg_quantity_per_purchase": round(avg_quantity, 1),
        "estimated_daily_consumption": round(daily_consumption, 4),
        "confidence_score": confidence,
        "last_purchased_date": last_purchase.order_date,
        "predicted_runout_date": runout_date,
        "next_purchase_date": next_purchase,
        "purchase_count": len(purchases),
    }


async def refresh_predictions(session: AsyncSession) -> int:
    """Recompute all consumption predictions. Returns count of predictions updated."""
    # Load all products with order history
    query = (
        select(Product)
        .join(OrderItem)
        .join(Order)
        .options(selectinload(Product.order_items).selectinload(OrderItem.order))
        .distinct()
    )
    result = await session.execute(query)
    products = {p.id: p for p in result.scalars().all()}

    # Load all confirmed/auto matches to merge purchase histories
    matches_result = await session.execute(select(ProductMatch))
    matches = matches_result.scalars().all()

    # Build: product_id -> canonical_id (lower id in pair), and canonical -> [all product ids in group]
    canonical_map: dict[int, int] = {}  # non-canonical -> canonical
    groups: dict[int, list[int]] = {}   # canonical -> [product_ids]

    for match in matches:
        a, b = match.product_a_id, match.product_b_id
        canon = min(a, b)
        other = max(a, b)
        canonical_map[other] = canon
        if canon not in groups:
            groups[canon] = [canon]
        if other not in groups[canon]:
            groups[canon].append(other)

    # Products not in any match are their own group
    for pid in products:
        if pid not in canonical_map and pid not in groups:
            groups[pid] = [pid]

    count = 0
    seen_non_canonical: set[int] = set()

    for canon_id, member_ids in groups.items():
        # Collect combined purchase records from all members that have order history
        purchases: list[PurchaseRecord] = []
        for pid in member_ids:
            product = products.get(pid)
            if product is None:
                continue
            for oi in product.order_items:
                purchases.append(
                    PurchaseRecord(order_date=oi.order.order_date, quantity=oi.quantity)
                )
            seen_non_canonical.update(m for m in member_ids if m != canon_id)

        if not purchases:
            continue

        pred_data = compute_prediction(purchases)
        if not pred_data:
            continue

        # Upsert prediction for the canonical product
        existing = await session.execute(
            select(ConsumptionPrediction).where(
                ConsumptionPrediction.product_id == canon_id
            )
        )
        pred = existing.scalar_one_or_none()

        if pred:
            for key, value in pred_data.items():
                setattr(pred, key, value)
        else:
            # Only create if the canonical product exists (has order history or is in products)
            canon_product = products.get(canon_id)
            if canon_product is None:
                # Canonical has no orders — use first member that does
                for pid in member_ids:
                    if pid in products:
                        canon_id = pid
                        break
                else:
                    continue
            pred = ConsumptionPrediction(product_id=canon_id, **pred_data)
            session.add(pred)

        # Remove stale predictions for non-canonical members
        for pid in member_ids:
            if pid == canon_id:
                continue
            stale = await session.execute(
                select(ConsumptionPrediction).where(ConsumptionPrediction.product_id == pid)
            )
            stale_pred = stale.scalar_one_or_none()
            if stale_pred:
                await session.delete(stale_pred)

        count += 1

    await session.commit()
    logger.info("Refreshed %d predictions", count)
    return count


def generate_candidates(
    predictions: list[ConsumptionPrediction],
    target_date: date | None = None,
    lookahead_days: int = 7,
    lead_time_days: int = 2,
    min_confidence: float = 0.3,
) -> list[ShoppingListCandidate]:
    """Generate shopping list candidates from predictions."""
    target_date = target_date or date.today()
    window_start = target_date - timedelta(days=lead_time_days)
    window_end = target_date + timedelta(days=lookahead_days)

    candidates = []
    for pred in predictions:
        if pred.confidence_score < min_confidence or pred.purchase_count < 2:
            continue
        if window_start <= pred.predicted_runout_date <= window_end:
            qty = math.ceil(pred.estimated_daily_consumption * lookahead_days)
            qty = max(qty, int(pred.avg_quantity_per_purchase))
            candidates.append(
                ShoppingListCandidate(
                    product_id=pred.product_id,
                    quantity=qty,
                    reason=f"Predicted runout: {pred.predicted_runout_date}",
                )
            )

    return candidates
