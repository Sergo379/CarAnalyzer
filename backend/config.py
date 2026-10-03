from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="CAR_ANALYZER_",
        extra="forbid",
    )

    app_name: str = "CarAnalyzer"
    environment: str = "development"

    database_path: Path = PROJECT_ROOT / "data" / "car_analyzer.db"
    catalog_runtime_path: Path = PROJECT_ROOT / "data" / "vehicle_catalog_runtime.json"
    avito_browser_data_path: Path = PROJECT_ROOT / "data" / "browser" / "avito"

    direct_price_percent: float = Field(default=0.07, ge=0, lt=1)
    expensive_max_percent: float = Field(default=0.20, gt=0, lt=1)
    cheaper_min_percent: float = Field(default=0.10, ge=0, lt=1)
    cheaper_max_percent: float = Field(default=0.20, gt=0, lt=1)

    auto_ru_base_url: str = "https://auto.ru"

    scraper_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    avito_browser_timeout_seconds: float = Field(default=8.0, gt=0, le=30)
    auto_ru_discovery_collection_seconds: float = Field(
        default=8.0,
        gt=0,
        le=30,
    )
    auto_ru_discovery_detail_seconds: float = Field(
        default=5.0,
        gt=0,
        le=30,
    )

    target_operation_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=180,
    )
    discovery_operation_timeout_seconds: float = Field(
        default=40.0,
        gt=0,
        le=180,
    )
    source_search_timeout_seconds: float = Field(
        default=42.0,
        gt=0,
        le=180,
    )
    search_total_timeout_seconds: float = Field(
        default=50.0,
        gt=0,
        le=240,
    )

    scraper_detail_concurrency: int = Field(default=4, ge=1, le=10)
    scraper_max_pages: int = Field(default=10, ge=1, le=50)
    live_search_max_pages: int = Field(default=2, ge=1, le=5)
    scraper_max_listings: int = Field(default=500, ge=1, le=5000)

    car_knowledge_ttl_days: int = Field(default=90, ge=1, le=3650)
    knowledge_max_search_queries: int = Field(default=10, ge=1, le=20)
    knowledge_search_results_per_query: int = Field(default=3, ge=1, le=10)
    knowledge_max_sources: int = Field(default=10, ge=1, le=30)
    knowledge_max_sources_per_domain: int = Field(default=2, ge=1, le=10)
    knowledge_source_timeout_seconds: float = Field(
        default=12.0,
        gt=0,
        le=60,
    )
    knowledge_discovery_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=120,
    )

    # Existing optional LLM integrations are retained for future adapters.
    yandex_api_key: str | None = None
    yandex_folder_id: str | None = None
    yandex_model: str = "yandexgpt/latest"

    gigachat_credentials: str | None = None
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat"

    # Active LLM provider
    llm_provider: str = Field(
        default="gemini",
        validation_alias="LLM_PROVIDER",
    )

    # Gemini
    gemini_api_key: str | None = Field(
        default=None,
        validation_alias="GEMINI_API_KEY",
    )
    gemini_model: str = Field(
        default="gemini-3.8-flash",
        validation_alias="GEMINI_MODEL",
    )
    gemini_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=240,
        validation_alias="GEMINI_TIMEOUT_SECONDS",
    )

    # Langfuse
    langfuse_public_key: str | None = Field(
        default=None,
        validation_alias="LANGFUSE_PUBLIC_KEY",
    )
    langfuse_secret_key: str | None = Field(
        default=None,
        validation_alias="LANGFUSE_SECRET_KEY",
    )
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias="LANGFUSE_HOST",
    )

    @model_validator(mode="after")
    def validate_price_thresholds(self) -> "Settings":
        if self.expensive_max_percent <= self.direct_price_percent:
            raise ValueError(
                "expensive_max_percent must exceed direct_price_percent"
            )

        if self.cheaper_max_percent < self.cheaper_min_percent:
            raise ValueError(
                "cheaper_max_percent must be >= cheaper_min_percent"
            )

        if self.search_total_timeout_seconds < self.source_search_timeout_seconds:
            raise ValueError(
                "search_total_timeout_seconds must be >= source_search_timeout_seconds"
            )

        return self

    def resolved_database_path(self) -> Path:
        return (
            self.database_path
            if self.database_path.is_absolute()
            else PROJECT_ROOT / self.database_path
        )

    def resolved_catalog_runtime_path(self) -> Path:
        return (
            self.catalog_runtime_path
            if self.catalog_runtime_path.is_absolute()
            else PROJECT_ROOT / self.catalog_runtime_path
        )

    def resolved_avito_browser_data_path(self) -> Path:
        return (
            self.avito_browser_data_path
            if self.avito_browser_data_path.is_absolute()
            else PROJECT_ROOT / self.avito_browser_data_path
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
