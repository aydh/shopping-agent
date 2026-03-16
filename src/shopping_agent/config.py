from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    data_dir: Path = Path(__file__).parent.parent.parent / "data"
    database_url: str = f"sqlite+aiosqlite:///{data_dir}/shopping_agent.db"
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False
    coles_api_key: str = Field(default="", description="Coles Ocp-Apim-Subscription-Key")

    def ensure_dirs(self) -> None:
        """Create required data directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)


# Matching / prediction thresholds
MIN_MATCH_CONFIDENCE: float = 0.3
FUZZY_MATCH_THRESHOLD: float = 70.0
FUZZY_SEARCH_THRESHOLD: float = 65.0
SIZE_MATCH_BONUS: int = 15
SIZE_MISMATCH_PENALTY: int = -20
BRAND_MATCH_THRESHOLD: float = 60.0

# Prediction parameters
PRODUCT_RECENCY_DAYS: int = 120
MIN_PREDICTION_CONFIDENCE: float = 0.3
PREDICTION_LOOKAHEAD_DAYS: int = 7
PREDICTION_LEAD_TIME_DAYS: int = 7
PREDICTION_PURCHASE_COUNT_MIN: int = 3

# Price refresh
PRICE_REFRESH_CONCURRENCY: int = 10

# Chart colours
COLES_COLOUR: str = "#dc2626"
WOOLWORTHS_COLOUR: str = "#16a34a"
PRICE_LINE_COLOUR: str = "#111827"


settings = Settings()
