from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_prefix="CAR_ANALYZER_", extra="ignore"
    )
    app_name: str = "CarAnalyzer"
    environment: str = "development"
    database_path: Path = PROJECT_ROOT / "data" / "car_analyzer.db"
    direct_price_percent: float = Field(default=0.07, ge=0, lt=1)
    expensive_max_percent: float = Field(default=0.20, gt=0, lt=1)
    cheaper_min_percent: float = Field(default=0.10, ge=0, lt=1)
    cheaper_max_percent: float = Field(default=0.20, gt=0, lt=1)
    auto_ru_base_url: str = "https://auto.ru"
    scraper_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    scraper_detail_concurrency: int = Field(default=4, ge=1, le=10)
    scraper_max_details: int = Field(default=40, ge=1, le=100)
    car_knowledge_ttl_days: int = Field(default=90, ge=1, le=3650)

    @model_validator(mode="after")
    def validate_price_thresholds(self) -> "Settings":
        if self.expensive_max_percent <= self.direct_price_percent:
            raise ValueError("expensive_max_percent must exceed direct_price_percent")
        if self.cheaper_max_percent < self.cheaper_min_percent:
            raise ValueError("cheaper_max_percent must be >= cheaper_min_percent")
        return self

    def resolved_database_path(self) -> Path:
        return (
            self.database_path
            if self.database_path.is_absolute()
            else PROJECT_ROOT / self.database_path
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
