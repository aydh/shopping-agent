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


def normalize_product_name(name: str) -> str:
    """Standardize product names for matching."""
    name = name.lower()
    # Remove store-specific prefixes
    for prefix in ["coles", "woolworths", "woolies"]:
        name = name.replace(prefix, "")
    # Normalize weight/volume formats
    name = re.sub(r"(\d+)\s*(g|kg|ml|l|pk|pack)\b", r"\1\2", name)
    return " ".join(name.split())


def normalize_size(size: str) -> str:
    """Normalize size strings for comparison."""
    size = size.lower().strip()
    size = re.sub(r"\s+", "", size)
    # Convert common variations
    size = size.replace("litre", "l").replace("liter", "l")
    size = size.replace("gram", "g").replace("kilogram", "kg")
    size = size.replace("millilitre", "ml").replace("milliliter", "ml")
    return size


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

        name_score = fuzz.token_sort_ratio(source_name, candidate_name)

        # Boost for matching sizes
        size_bonus = 0
        if source.unit_size and candidate.unit_size:
            if normalize_size(source.unit_size) == normalize_size(candidate.unit_size):
                size_bonus = 15

        final_score = min(name_score + size_bonus, 100)

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
    # Check existing matches
    if product.store == Store.COLES:
        existing = await session.execute(
            select(ProductMatch)
            .where(ProductMatch.product_a_id == product.id)
            .join(Product, ProductMatch.product_b_id == Product.id)
            .where(Product.store == target_store)
        )
    else:
        existing = await session.execute(
            select(ProductMatch)
            .where(ProductMatch.product_b_id == product.id)
            .join(Product, ProductMatch.product_a_id == Product.id)
            .where(Product.store == target_store)
        )

    match = existing.scalar_one_or_none()
    if match:
        return match

    # Try to find a match in local DB
    candidates_result = await session.execute(
        select(Product).where(Product.store == target_store)
    )
    candidates = list(candidates_result.scalars().all())

    local_match = find_best_match(product, candidates)
    if local_match:
        matched_product, confidence = local_match
        pm = ProductMatch(
            product_a_id=min(product.id, matched_product.id),
            product_b_id=max(product.id, matched_product.id),
            confidence=confidence,
            match_method="fuzzy_name",
        )
        session.add(pm)
        await session.flush()
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

                search_match = find_best_match(product, search_products, threshold=65.0)
                if search_match:
                    matched_product, confidence = search_match
                    pm = ProductMatch(
                        product_a_id=min(product.id, matched_product.id),
                        product_b_id=max(product.id, matched_product.id),
                        confidence=confidence,
                        match_method="search",
                    )
                    session.add(pm)
                    await session.flush()
                    return pm
        except Exception:
            logger.debug("Search-based matching failed for %s", product.name)

    return None


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
