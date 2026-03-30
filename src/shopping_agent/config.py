from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Display timezone — used for log messages and template date rendering
APP_TIMEZONE = ZoneInfo("Australia/Sydney")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    data_dir: Path = Path(__file__).parent.parent.parent / "data"
    database_url: str  # e.g. postgresql+asyncpg://postgres:password@db.<ref>.supabase.co:5432/postgres
    log_dir: Path = Path(__file__).parent.parent.parent / "logs"
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False
    coles_api_key: str | None = Field(default=None, description="Coles Ocp-Apim-Subscription-Key (COLES_API_KEY)")
    woolworths_api_key: str | None = Field(default=None, description="Woolworths mobile API key (WOOLWORTHS_API_KEY)")
    ssl_certfile: Path | None = Field(default=None, description="Path to SSL certificate file (SSL_CERTFILE)")
    ssl_keyfile: Path | None = Field(default=None, description="Path to SSL private key file (SSL_KEYFILE)")
    supabase_jwt_secret: str = Field(default="", description="Supabase JWT secret for token verification")
    supabase_url: str | None = Field(default=None, description="Supabase project URL")
    supabase_anon_key: str | None = Field(default=None, description="Supabase anon/public key")
    base_url: str = Field(default="https://localhost:8000", description="Public HTTPS URL of this app")
    mcp_oauth_client_id: str = Field(default="", description="OAuth client ID registered with Supabase for MCP")
    mcp_oauth_client_secret: str = Field(default="", description="OAuth client secret for MCP")
    mcp_jwt_algorithm: str = Field(default="ES256", description="JWT algorithm Supabase uses (ES256/RS256 use JWKS; HS256 uses SUPABASE_JWT_SECRET)")
    enable_scheduler: bool = Field(default=False, description="Enable scheduled price refresh (ENABLE_SCHEDULER)")
    playwright_profile_dir: str | None = Field(default=None, description="Persistent Chrome profile dir for Playwright login (PLAYWRIGHT_PROFILE_DIR)")
    playwright_headless: bool = Field(default=True, description="Run Playwright browser in headless mode (PLAYWRIGHT_HEADLESS)")
    playwright_channel: str | None = Field(default=None, description="Playwright browser channel, e.g. 'chrome' for system Chrome (PLAYWRIGHT_CHANNEL)")

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
MIN_MATCH_CONFIDENCE: float = 0.4

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
SIZE_MATCH_BONUS: int = 25

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
PRODUCT_RECENCY_DAYS: int = 365

# MIN_PREDICTION_CONFIDENCE  Predictions below this confidence are excluded from
#                            shopping list generation. Raise to 0.5+ to only
#                            include high-certainty predictions.
MIN_PREDICTION_CONFIDENCE: float = 0.4

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
PREDICTION_PURCHASE_COUNT_MIN: int = 3

# Price refresh
# KNOWNN WORKING SETTINGS FOR ~400 prods 10, 0
COLES_PRICE_REFRESH_CONCURRENCY: int = 5
COLES_PRICE_FETCH_DELAY_S: float = 0.0      # Delay between individual Coles product price requests
# KNOWNN WORKING SETTINGS FOR ~400 prods 2, 0.15, 0.05
WOOLWORTHS_PRICE_REFRESH_CONCURRENCY: int = 2
WOOLWORTHS_PRICE_FETCH_DELAY_S: float = 0.15  # Delay between individual Woolworths product price requests
WOOLWORTHS_PRICE_FETCH_JITTER_S: float = 0.05  # Max random jitter added on top of delay (uniform 0–jitter)

# Scheduled price refresh
PRICE_REFRESH_INTERVAL_HOURS: int = 4        # How often to run the scheduled refresh
PRICE_REFRESH_JITTER_MINUTES: int = 60       # Max random offset (±minutes) applied to each scheduled run

# ── Playwright login delays ───────────────────────────────────────────────────
#
# All values are in milliseconds. Increase if Incapsula or the Coles auth
# page rejects requests that arrive too quickly.
#
# PLAYWRIGHT_DELAY_AFTER_HOMEPAGE_MS  Pause after homepage networkidle before
#                                     navigating to the login page. Gives
#                                     Incapsula time to set its session cookie.
PLAYWRIGHT_DELAY_AFTER_HOMEPAGE_MS: int = 1000

# PLAYWRIGHT_DELAY_AFTER_EMAIL_MS  Pause between filling email and password.
PLAYWRIGHT_DELAY_AFTER_EMAIL_MS: int = 1000

# PLAYWRIGHT_DELAY_AFTER_PASSWORD_MS  Pause after filling password before
#                                     clicking the Log In button.
PLAYWRIGHT_DELAY_AFTER_PASSWORD_MS: int = 1000

# PLAYWRIGHT_DELAY_AFTER_MFA_MS  Pause after filling the MFA code before
#                                clicking Continue.
PLAYWRIGHT_DELAY_AFTER_MFA_MS: int = 1000

# Chart colours
COLES_COLOUR: str = "#dc2626"
WOOLWORTHS_COLOUR: str = "#16a34a"
PRICE_LINE_COLOUR: str = "#111827"


settings = Settings()
