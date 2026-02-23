from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    data_dir: Path = Path(__file__).parent.parent.parent / "data"
    database_url: str = f"sqlite+aiosqlite:///{data_dir}/shopping_agent.db"
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
