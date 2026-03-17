from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    data_dir: Path = Path(__file__).parent.parent.parent / "data"
    database_url: str = f"sqlite+aiosqlite:///{data_dir}/shopping_agent.db"
    log_dir: Path = Path(__file__).parent.parent.parent / "logs"
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False
    coles_api_key: str | None = Field(default=None, description="Coles Ocp-Apim-Subscription-Key (COLES_API_KEY)")
    price_refresh_poll_interval_ms: int = 1000

    def ensure_dirs(self) -> None:
        """Create required data directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


# ── Product matching thresholds ──────────────────────────────────────────────
#
# Scores are 0–100. Name similarity uses rapidfuzz token_sort_ratio and
# token_set_ratio (max of both), then a size adjustment is applied.
#
# MIN_MATCH_CONFIDENCE  Lower bound used in a few places as a floor; not the
#                       primary gate. Raise to 0.4–0.5 to discard low-quality
#                       auto-matches from being used in price comparisons.
MIN_MATCH_CONFIDENCE: float = 0.3

# FUZZY_MATCH_THRESHOLD  Minimum score for local-DB fuzzy matching. This is
#                        the main dial for match quality vs. coverage:
#                          70 (default) – good balance; some borderline matches
#                          75–80        – fewer matches, higher precision
#                          65           – more matches, more false positives
FUZZY_MATCH_THRESHOLD: float = 80.0

# FUZZY_SEARCH_THRESHOLD  Same gate but applied to live scraper search results.
#                         Slightly lower than FUZZY_MATCH_THRESHOLD because the
#                         search engine already pre-filters by relevance.
#                         Keep it ≤ FUZZY_MATCH_THRESHOLD.
FUZZY_SEARCH_THRESHOLD: float = 65.0

# SIZE_MATCH_BONUS  Added to the name score when both unit_size fields parse to
#                   the same value (e.g. 500g == 500g). Rewards size agreement.
#                   Raise to 20–25 to make size more decisive.
SIZE_MATCH_BONUS: int = 15

# SIZE_MISMATCH_PENALTY  Subtracted when both sizes are parseable but differ
#                        (e.g. 250g vs 1kg). Deliberately larger than the bonus
#                        to strongly penalise size mismatches.
#                        Raise the magnitude (e.g. -30) if wrong-size matches
#                        are still slipping through.
SIZE_MISMATCH_PENALTY: int = -30

# BRAND_MATCH_THRESHOLD  Minimum fuzz.ratio score between brand strings before
#                        a candidate is even considered for name scoring. Acts
#                        as a hard gate: candidates below this are skipped
#                        entirely, preventing cross-brand false positives.
#                          60 (default) – allows minor brand name variations
#                          70–75        – stricter; require closer brand match
#                          0            – disable brand gating entirely
BRAND_MATCH_THRESHOLD: float = 70.0

# ── Consumption prediction parameters ────────────────────────────────────────
#
# PRODUCT_RECENCY_DAYS  Only order history within this window is used to build
#                       consumption predictions. Lower values make predictions
#                       more sensitive to recent behaviour; higher values smooth
#                       over seasonal gaps.
PRODUCT_RECENCY_DAYS: int = 90

# MIN_PREDICTION_CONFIDENCE  Predictions below this confidence are excluded from
#                            shopping list generation. Raise to 0.5+ to only
#                            include high-certainty predictions.
MIN_PREDICTION_CONFIDENCE: float = 0.5

# PREDICTION_LOOKAHEAD_DAYS  How far ahead of today to look when generating
#                            candidates. Items predicted to run out within this
#                            window are included in the shopping list.
PREDICTION_LOOKAHEAD_DAYS: int = 7

# PREDICTION_LEAD_TIME_DAYS  Items predicted to run out within this many days
#                            *before* today are also included (catches items
#                            already overdue). Raise if you often run out before
#                            the list is generated.
PREDICTION_LEAD_TIME_DAYS: int = 7

# PREDICTION_PURCHASE_COUNT_MIN  Minimum number of past purchases required
#                                before a prediction is generated for a product.
#                                Raise to 4–5 to require more purchase history
#                                before trusting the interval estimate.
PREDICTION_PURCHASE_COUNT_MIN: int = 4

# Price refresh
PRICE_REFRESH_CONCURRENCY: int = 5

# Chart colours
COLES_COLOUR: str = "#dc2626"
WOOLWORTHS_COLOUR: str = "#16a34a"
PRICE_LINE_COLOUR: str = "#111827"


settings = Settings()
