import logging
import re
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Product, ProductMatch, Store
from ..scrapers.base import BaseScraper, ScrapedProduct

logger = logging.getLogger(__name__)


@dataclass
class PriceComparison:
    product_name: str
    unit_size: str | None
    product_id: int
    coles_product: Product | None
    woolworths_product: Product | None
    coles_price: float | None
    woolworths_price: float | None
    cheaper_store: Store | None
    savings: float
    match_id: int | None
    match_confidence: float | None
    is_confirmed: bool
    match_method: str | None = None


def normalize_product_name(name: str) -> str:
    """Standardize product names for matching."""
    name = name.lower()
    # Remove store-specific words
    for word in ["coles", "woolworths", "woolies"]:
        name = re.sub(rf"\b{word}\b", "", name)
    # Remove common descriptor words that vary between stores
    for word in [
        "fresh", "australian", "australia", "free range", "free-range",
        "organic", "no added", "value", "selected", "varieties", "variety",
    ]:
        name = name.replace(word, "")
    # Normalize weight/volume formats (remove spaces between number and unit)
    name = re.sub(r"(\d+)\s*(g|kg|ml|l|pk|pack)\b", r"\1\2", name)
    return " ".join(name.split())


def normalize_size(size: str) -> str:
    """Normalize size strings for comparison."""
    size = size.lower().strip()
    size = re.sub(r"\s+", "", size)
    size = size.replace("litre", "l").replace("liter", "l")
    size = size.replace("gram", "g").replace("kilogram", "kg")
    size = size.replace("millilitre", "ml").replace("milliliter", "ml")
    return size


def size_to_grams(size: str) -> float | None:
    """Convert a size string to a canonical numeric value in base units (g or ml).

    Returns None if the size can't be parsed.
    """
    s = normalize_size(size)
    m = re.match(r"^([\d.]+)(g|kg|ml|l)$", s)
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2)
    if unit == "kg":
        return value * 1000
    if unit == "l":
        return value * 1000
    return value  # g or ml already in base unit


def sizes_compatible(a: str | None, b: str | None) -> int:
    """Compare two size strings. Returns:
      +15  if sizes match (same product size)
      -20  if sizes are both parseable and clearly differ
        0  if sizes are missing or unparseable (can't judge)
    """
    if not a or not b:
        return 0
    # Try exact string match first (handles multi-packs like "3pk" etc.)
    if normalize_size(a) == normalize_size(b):
        return 15
    # Try numeric comparison
    av, bv = size_to_grams(a), size_to_grams(b)
    if av is not None and bv is not None:
        if abs(av - bv) < 1:  # essentially equal (float rounding)
            return 15
        return -20  # both parseable but genuinely different sizes
    return 0


def find_best_match(
    source: Product,
    candidates: list[Product],
    threshold: float = 70.0,
) -> tuple[Product, float] | None:
    """Find the best matching product from candidates."""
    source_name = normalize_product_name(source.name)
    best_match = None
    best_score = 0.0

    for candidate in candidates:
        candidate_name = normalize_product_name(candidate.name)

        # Filter by brand first if available
        if source.brand and candidate.brand:
            brand_score = fuzz.ratio(source.brand.lower(), candidate.brand.lower())
            if brand_score < 60:
                continue

        # Use both algorithms — token_set handles subset matches (e.g. "milk full cream 2L" vs "full cream milk 2L")
        name_score = max(
            fuzz.token_sort_ratio(source_name, candidate_name),
            fuzz.token_set_ratio(source_name, candidate_name),
        )

        size_adjustment = sizes_compatible(source.unit_size, candidate.unit_size)
        final_score = min(max(name_score + size_adjustment, 0), 100)

        if final_score > best_score and final_score >= threshold:
            best_score = final_score
            best_match = candidate

    if best_match:
        return best_match, best_score / 100.0
    return None


async def find_or_create_match(
    session: AsyncSession,
    product: Product,
    target_store: Store,
    scraper: BaseScraper | None = None,
) -> ProductMatch | None:
    """Find existing match or discover one via search."""
    # Find all existing matches for this product (in either position)
    existing_result = await session.execute(
        select(ProductMatch).where(
            (ProductMatch.product_a_id == product.id) | (ProductMatch.product_b_id == product.id)
        )
    )
    existing_matches = existing_result.scalars().all()

    rejected_partner_ids: set[int] = set()
    for m in existing_matches:
        partner_id = m.product_b_id if m.product_a_id == product.id else m.product_a_id
        if m.is_rejected:
            rejected_partner_ids.add(partner_id)
        else:
            return m  # Active match found

    # Try to find a match in local DB
    candidates_result = await session.execute(
        select(Product).where(Product.store == target_store)
    )
    candidates = [p for p in candidates_result.scalars().all() if p.id not in rejected_partner_ids]

    local_match = find_best_match(product, candidates)
    if local_match:
        matched_product, confidence = local_match
        pm = await _insert_match_or_fetch_existing(
            session, product.id, matched_product.id, confidence, "fuzzy_name"
        )
        return pm

    # Try search via scraper if available
    if scraper:
        search_query = f"{product.brand or ''} {product.name}".strip()
        try:
            results = await scraper.search_product(search_query)
            if results:
                # Convert to Product objects for matching
                search_products = []
                for r in results:
                    p = await _upsert_scraped_product(session, r, target_store)
                    search_products.append(p)

                search_products = [p for p in search_products if p.id not in rejected_partner_ids]
                search_match = find_best_match(product, search_products, threshold=65.0)
                if search_match:
                    matched_product, confidence = search_match
                    pm = await _insert_match_or_fetch_existing(
                        session, product.id, matched_product.id, confidence, "search"
                    )
                    return pm
        except Exception:
            logger.debug("Search-based matching failed for %s", product.name)

    return None


async def _insert_match_or_fetch_existing(
    session: AsyncSession,
    product_id: int,
    matched_id: int,
    confidence: float,
    method: str,
) -> ProductMatch | None:
    """Insert a new ProductMatch, or return the existing one on conflict.

    Returns None if the existing match is rejected (should not be re-used).
    Uses a savepoint so a conflict doesn't roll back the surrounding transaction.
    """
    from sqlalchemy.exc import IntegrityError

    a_id, b_id = min(product_id, matched_id), max(product_id, matched_id)
    pm = ProductMatch(
        product_a_id=a_id,
        product_b_id=b_id,
        confidence=confidence,
        match_method=method,
    )
    try:
        async with session.begin_nested():
            session.add(pm)
            await session.flush()
        return pm
    except IntegrityError:
        result = await session.execute(
            select(ProductMatch).where(
                ProductMatch.product_a_id == a_id,
                ProductMatch.product_b_id == b_id,
            )
        )
        existing = result.scalar_one()
        return None if existing.is_rejected else existing


async def _upsert_scraped_product(
    session: AsyncSession, scraped: ScrapedProduct, store: Store
) -> Product:
    """Upsert a scraped product into the database."""
    result = await session.execute(
        select(Product).where(
            Product.store == store,
            Product.store_product_id == scraped.store_product_id,
        )
    )
    product = result.scalar_one_or_none()

    if product:
        product.current_price = scraped.current_price
        product.is_available = scraped.is_available
        if scraped.unit_price:
            product.unit_price = scraped.unit_price
    else:
        product = Product(
            store=store,
            store_product_id=scraped.store_product_id,
            name=scraped.name,
            brand=scraped.brand,
            category=scraped.category,
            unit_size=scraped.unit_size,
            current_price=scraped.current_price,
            unit_price=scraped.unit_price,
            unit_price_measure=scraped.unit_price_measure,
            image_url=scraped.image_url,
            product_url=scraped.product_url,
            is_available=scraped.is_available,
        )
        session.add(product)
        await session.flush()

    return product


async def match_unmatched_products(session: AsyncSession, store: Store) -> int:
    """Run auto-matching for all unmatched products from the given store. Returns match count."""
    target_store = Store.WOOLWORTHS if store == Store.COLES else Store.COLES

    # Find products from this store that have no existing active match
    already_matched = await session.execute(
        select(ProductMatch.product_a_id, ProductMatch.product_b_id)
        .where(ProductMatch.is_rejected == False)  # noqa: E712
    )
    matched_ids: set[int] = set()
    for a_id, b_id in already_matched.all():
        matched_ids.add(a_id)
        matched_ids.add(b_id)

    unmatched_result = await session.execute(
        select(Product).where(Product.store == store)
    )
    unmatched = [p for p in unmatched_result.scalars().all() if p.id not in matched_ids]

    candidates_result = await session.execute(
        select(Product).where(Product.store == target_store)
    )
    all_candidates = list(candidates_result.scalars().all())

    # Load all rejected pairs so we don't re-match them
    unmatched_ids = {p.id for p in unmatched}
    rejected_result = await session.execute(
        select(ProductMatch.product_a_id, ProductMatch.product_b_id)
        .where(ProductMatch.is_rejected == True)  # noqa: E712
    )
    # Map: product_id -> set of partner ids that are rejected
    rejected_partners: dict[int, set[int]] = {}
    for a_id, b_id in rejected_result.all():
        if a_id in unmatched_ids:
            rejected_partners.setdefault(a_id, set()).add(b_id)
        if b_id in unmatched_ids:
            rejected_partners.setdefault(b_id, set()).add(a_id)

    candidates = list(all_candidates)
    count = 0
    for product in unmatched:
        rejected = rejected_partners.get(product.id, set())
        eligible = [c for c in candidates if c.id not in rejected]
        local_match = find_best_match(product, eligible)
        if local_match:
            matched_product, confidence = local_match
            pm = ProductMatch(
                product_a_id=min(product.id, matched_product.id),
                product_b_id=max(product.id, matched_product.id),
                confidence=confidence,
                match_method="fuzzy_name",
            )
            session.add(pm)
            # Add to matched_ids so the same target product isn't matched twice
            matched_ids.add(product.id)
            matched_ids.add(matched_product.id)
            # Remove the matched candidate to prevent duplicate pairings
            candidates = [c for c in candidates if c.id != matched_product.id]
            count += 1

    if count:
        await session.commit()
        logger.info("Auto-matched %d products from %s", count, store.value)

    return count


async def compare_product_prices(
    session: AsyncSession,
    product_ids: list[int],
) -> list[PriceComparison]:
    """Compare prices for a list of products across both stores."""
    comparisons = []

    for pid in product_ids:
        product = await session.get(Product, pid)
        if not product:
            continue

        target_store = (
            Store.WOOLWORTHS if product.store == Store.COLES else Store.COLES
        )

        match = await find_or_create_match(session, product, target_store)

        coles_product = None
        woolworths_product = None
        coles_price = None
        woolworths_price = None

        if product.store == Store.COLES:
            coles_product = product
            coles_price = product.current_price
            if match:
                other_id = (
                    match.product_b_id
                    if match.product_a_id == product.id
                    else match.product_a_id
                )
                woolworths_product = await session.get(Product, other_id)
                if woolworths_product:
                    woolworths_price = woolworths_product.current_price
        else:
            woolworths_product = product
            woolworths_price = product.current_price
            if match:
                other_id = (
                    match.product_a_id
                    if match.product_b_id == product.id
                    else match.product_b_id
                )
                coles_product = await session.get(Product, other_id)
                if coles_product:
                    coles_price = coles_product.current_price

        cheaper = None
        savings = 0.0
        if coles_price and woolworths_price:
            if coles_price < woolworths_price:
                cheaper = Store.COLES
                savings = woolworths_price - coles_price
            elif woolworths_price < coles_price:
                cheaper = Store.WOOLWORTHS
                savings = coles_price - woolworths_price

        comparisons.append(
            PriceComparison(
                product_name=product.name,
                unit_size=product.unit_size,
                product_id=product.id,
                coles_product=coles_product,
                woolworths_product=woolworths_product,
                coles_price=coles_price,
                woolworths_price=woolworths_price,
                cheaper_store=cheaper,
                savings=savings,
                match_id=match.id if match else None,
                match_confidence=match.confidence if match else None,
                is_confirmed=match.is_confirmed if match else False,
            )
        )

    return comparisons


def matches_to_comparisons(matches: list[ProductMatch]) -> list[PriceComparison]:
    """Convert a list of ProductMatch ORM rows into PriceComparison dataclasses.

    Args:
        matches: ProductMatch rows with product_a and product_b eagerly loaded.

    Returns:
        List of PriceComparison dataclasses ready for template rendering.
    """
    comparisons = []
    for match in matches:
        pa, pb = match.product_a, match.product_b
        coles_p = pa if pa.store == Store.COLES else pb
        ww_p = pa if pa.store == Store.WOOLWORTHS else pb

        cp = coles_p.current_price
        wp = ww_p.current_price
        cheaper: Store | None = None
        savings = 0.0
        if cp and wp:
            if cp < wp:
                cheaper = Store.COLES
                savings = wp - cp
            elif wp < cp:
                cheaper = Store.WOOLWORTHS
                savings = cp - wp

        comparisons.append(
            PriceComparison(
                product_name=coles_p.name,
                unit_size=coles_p.unit_size,
                product_id=coles_p.id,
                coles_product=coles_p,
                woolworths_product=ww_p,
                coles_price=cp,
                woolworths_price=wp,
                cheaper_store=cheaper,
                savings=savings,
                match_id=match.id,
                match_confidence=match.confidence,
                is_confirmed=match.is_confirmed,
                match_method=match.match_method,
            )
        )
    return comparisons


def build_price_map(matches: list[ProductMatch]) -> dict[int, dict[str, float | None]]:
    """Build a product_id → {coles_price, woolworths_price} lookup from match rows.

    Both product_a and product_b must be eagerly loaded on each match.
    Both products in a pair share the same entry so either product_id can be used
    as the key.

    Args:
        matches: ProductMatch rows with product_a and product_b eagerly loaded.

    Returns:
        Dict mapping product_id to a dict with 'coles_price' and 'woolworths_price'.
    """
    price_map: dict[int, dict[str, float | None]] = {}
    for match in matches:
        pa, pb = match.product_a, match.product_b
        coles_p = pa if pa.store == Store.COLES else pb
        ww_p = pa if pa.store == Store.WOOLWORTHS else pb
        entry: dict[str, float | None] = {
            "coles_price": coles_p.current_price,
            "woolworths_price": ww_p.current_price,
        }
        price_map[coles_p.id] = entry
        price_map[ww_p.id] = entry
    return price_map
