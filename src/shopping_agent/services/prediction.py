import logging
import math
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, stdev

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import (
    MIN_PREDICTION_CONFIDENCE,
    PREDICTION_LEAD_TIME_DAYS,
    PREDICTION_LOOKAHEAD_DAYS,
    PREDICTION_PURCHASE_COUNT_MIN,
    PRODUCT_RECENCY_DAYS,
)
from ..models import ConsumptionPrediction, Order, OrderItem, Product, ProductMatch, UserProductPreferences

logger = logging.getLogger(__name__)


@dataclass
class PurchaseRecord:
    order_date: date
    quantity: int
    store: str = ""


@dataclass
class ShoppingListCandidate:
    product_id: int
    quantity: int
    reason: str


@dataclass
class PredictionView:
    """Immutable view over a ConsumptionPrediction with pre-computed match info.

    Returned by get_predictions_with_match_info to avoid mutating ORM objects
    with transient attributes that have no column mapping.
    """

    # Forwarded from ORM columns
    product_id: int
    product: Product
    predicted_runout_date: date
    estimated_daily_consumption: float
    confidence_score: float
    last_purchased_date: date
    last_purchase_store: str
    last_purchase_quantity: int
    # Computed fields
    days_until_runout: int
    is_matched: bool
    matched_product: Product | None
    match_id: int | None


def compute_prediction(
    purchases: list[PurchaseRecord],
    today: date | None = None,
    decay_factor: float = 0.3,
    lead_time_days: int = 2,
) -> dict | None:
    """Compute consumption prediction stats for a single product.

    Analyzes purchase history to estimate daily consumption, predict runout
    dates, and compute confidence scores. Uses exponential decay weighting
    to favor recent purchases, normalizes intervals by quantity purchased,
    and filters out non-positive intervals.

    Args:
        purchases: List of PurchaseRecord objects sorted by date.
        today: Reference date for calculations (defaults to today).
        decay_factor: Exponential decay weight; controls how strongly recent
            purchases are weighted vs. older ones. Typically 0.1-0.5; higher
            values weight recent purchases more heavily.
        lead_time_days: Days before predicted runout to trigger next purchase
            date (e.g., 2 days means reorder 2 days before estimated runout).

    Returns:
        Dict with keys: avg_purchase_interval_days, avg_quantity_per_purchase,
        estimated_daily_consumption, confidence_score, last_purchased_date,
        predicted_runout_date, next_purchase_date, purchase_count,
        last_purchase_quantity, last_purchase_store. Returns None if fewer
        than 2 purchases, no positive intervals, or calculated consumption <= 0.

    Edge cases:
        - Zero or negative inter-purchase intervals are skipped.
        - Confidence scores incorporate data volume and purchase regularity
          (coefficient of variation).
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
    regularity_confidence = 1 / (1 + interval_cv)
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
        "last_purchase_quantity": last_purchase.quantity,
        "last_purchase_store": last_purchase.store,
    }


async def refresh_predictions(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Recompute all consumption predictions and persist them to the database.

    Groups products by their cross-store equivalency using union-find so that
    purchase histories are merged correctly across matched products. Predictions
    older than PRODUCT_RECENCY_DAYS are removed. The prediction is stored under
    the canonical (lowest-id) product in each group.

    Args:
        session: Async database session.

    Returns:
        Number of ConsumptionPrediction rows created or updated.
    """
    # Load all products with order history (hiding is per-user, not global)
    query = (
        select(Product)
        .join(OrderItem)
        .join(Order)
        .options(selectinload(Product.order_items).selectinload(OrderItem.order))
        .distinct()
    )
    result = await session.execute(query)
    products = {p.id: p for p in result.scalars().all()}

    # Load all non-rejected matches to merge purchase histories
    matches_result = await session.execute(
        select(ProductMatch).where(ProductMatch.is_rejected == False)  # noqa: E712
    )
    matches = matches_result.scalars().all()

    # Union-find to correctly group transitively-matched products.
    # A product may appear in multiple matches (e.g. A↔B and B↔C),
    # so simple pairwise min-id grouping breaks down — B ends up in
    # two separate groups and its purchases get double-counted while
    # stale predictions are never cleaned up.
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        """Return root representative of x using path compression."""
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])  # path compression
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for match in matches:
        union(match.product_a_id, match.product_b_id)

    # Build groups: root canonical -> [all member product ids with order history]
    groups: dict[int, list[int]] = {}
    for pid in products:
        canon = find(pid)
        if canon not in groups:
            groups[canon] = []
        if pid not in groups[canon]:
            groups[canon].append(pid)

    # Load product IDs this user has opted out of predictions for, or hidden
    excluded_result = await session.execute(
        select(UserProductPreferences.product_id)
        .where(
            UserProductPreferences.user_id == user_id,
            (UserProductPreferences.exclude_from_predictions.is_(True) | UserProductPreferences.is_hidden.is_(True)),
        )
    )
    excluded_product_ids = {row[0] for row in excluded_result.all()}

    # Bulk-fetch all existing predictions upfront — keyed by product_id
    existing_preds: dict[int, ConsumptionPrediction] = {
        p.product_id: p
        for p in (await session.execute(
            select(ConsumptionPrediction).where(ConsumptionPrediction.user_id == user_id)
        )).scalars().all()
    }

    count = 0
    today = date.today()
    recency_cutoff = today - timedelta(days=PRODUCT_RECENCY_DAYS)  # 4 months

    for initial_canon_id, member_ids in groups.items():
        # Collect combined purchase records from all members that have order history
        purchases: list[PurchaseRecord] = []
        for pid in member_ids:
            product = products.get(pid)
            if product is None:
                continue
            for oi in product.order_items:
                purchases.append(
                    PurchaseRecord(order_date=oi.order.order_date, quantity=oi.quantity, store=product.store.value)
                )

        if not purchases:
            continue

        # Skip and remove prediction if the user has opted out for any member
        if excluded_product_ids.intersection(member_ids):
            for pid in member_ids:
                if pid in existing_preds:
                    await session.delete(existing_preds.pop(pid))
            continue

        # Skip and remove prediction if not purchased in the last 4 months
        most_recent = max(p.order_date for p in purchases)
        if most_recent < recency_cutoff:
            for pid in member_ids:
                if pid in existing_preds:
                    await session.delete(existing_preds.pop(pid))
            continue

        # Only use recent purchases for the prediction to keep intervals accurate
        purchases = [p for p in purchases if p.order_date >= recency_cutoff]

        pred_data = compute_prediction(purchases)
        if not pred_data:
            continue

        # Determine which product to store the prediction under.
        # Prefer the canonical (lower id), but fall back to the first member with orders
        # if the canonical has no order history. Must be resolved BEFORE querying for
        # existing predictions to avoid a duplicate-key error on subsequent refreshes.
        canon_id = initial_canon_id
        if canon_id not in products:
            for pid in member_ids:
                if pid in products:
                    canon_id = pid
                    break
            else:
                continue  # no member has order history (shouldn't happen)

        # Upsert prediction for the canonical product
        pred = existing_preds.get(canon_id)
        if pred:
            for key, value in pred_data.items():
                setattr(pred, key, value)
        else:
            pred = ConsumptionPrediction(product_id=canon_id, user_id=user_id, **pred_data)
            session.add(pred)

        # Remove stale predictions for non-canonical members
        for pid in member_ids:
            if pid != canon_id and pid in existing_preds:
                await session.delete(existing_preds.pop(pid))

        count += 1

    await session.commit()
    logger.info("Refreshed %d predictions", count)
    return count


async def get_predictions_with_match_info(
    session: AsyncSession,
    user_id: uuid.UUID,
    max_runout_date: date | None = None,
) -> list[PredictionView]:
    """Load all predictions as PredictionView objects with pre-computed match info.

    Args:
        session: Async database session.
        max_runout_date: If provided, only return predictions with runout date <= this.

    Returns:
        List of PredictionView objects ordered by runout date.
    """
    today = date.today()
    query = (
        select(ConsumptionPrediction)
        .options(selectinload(ConsumptionPrediction.product))
        .where(ConsumptionPrediction.user_id == user_id)
        .order_by(ConsumptionPrediction.predicted_runout_date)
    )
    if max_runout_date is not None:
        query = query.where(ConsumptionPrediction.predicted_runout_date <= max_runout_date)
    result = await session.execute(query)

    matches_result = await session.execute(
        select(ProductMatch)
        .where(ProductMatch.is_rejected == False)  # noqa: E712
        .options(
            selectinload(ProductMatch.product_a),
            selectinload(ProductMatch.product_b),
        )
    )
    matched_product: dict[int, Product] = {}
    match_id_map: dict[int, int] = {}
    for m in matches_result.scalars().all():
        matched_product[m.product_a_id] = m.product_b
        matched_product[m.product_b_id] = m.product_a
        match_id_map[m.product_a_id] = m.id
        match_id_map[m.product_b_id] = m.id

    predictions = []
    for pred in result.scalars().all():
        other = matched_product.get(pred.product_id)
        predictions.append(PredictionView(
            product_id=pred.product_id,
            product=pred.product,
            predicted_runout_date=pred.predicted_runout_date,
            estimated_daily_consumption=pred.estimated_daily_consumption,
            confidence_score=pred.confidence_score,
            last_purchased_date=pred.last_purchased_date,
            last_purchase_store=pred.last_purchase_store,
            last_purchase_quantity=pred.last_purchase_quantity,
            days_until_runout=(pred.predicted_runout_date - today).days,
            is_matched=other is not None,
            matched_product=other,
            match_id=match_id_map.get(pred.product_id),
        ))
    return predictions


def generate_candidates(
    predictions: list[ConsumptionPrediction],
    target_date: date | None = None,
    lookahead_days: int = PREDICTION_LOOKAHEAD_DAYS,
    lead_time_days: int = PREDICTION_LEAD_TIME_DAYS,
    min_confidence: float = MIN_PREDICTION_CONFIDENCE,
) -> list[ShoppingListCandidate]:
    """Generate shopping list candidates from predictions."""
    target_date = target_date or date.today()
    window_start = target_date - timedelta(days=lead_time_days)
    window_end = target_date + timedelta(days=lookahead_days)

    candidates = []
    for pred in predictions:
        if pred.confidence_score < min_confidence or pred.purchase_count < PREDICTION_PURCHASE_COUNT_MIN:
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
