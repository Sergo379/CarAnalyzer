from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from backend.models.car import BodyType, Transmission


class ListingStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    NOT_FOUND = "not_found"
    REMOVED = "removed"


class CarListing(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    source: str = Field(min_length=1, max_length=40)
    external_id: str = Field(min_length=1, max_length=255)
    brand: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    canonical_brand_id: str | None = None
    canonical_model_id: str | None = None
    canonical_generation_id: str | None = None
    canonical_modification_id: str | None = None
    modification: str | None = Field(default=None, max_length=300)
    generation: str | None = Field(default=None, max_length=160)
    fuel_type: str | None = Field(default=None, max_length=40)
    engine_displacement: float | None = Field(default=None, gt=0)
    power_hp: int | None = Field(default=None, gt=0)
    engine_code: str | None = Field(default=None, max_length=80)
    drivetrain: str | None = Field(default=None, max_length=40)
    year: int = Field(ge=1886, le=2100)
    body_type: BodyType | None = None
    transmission: Transmission | None = None
    price: int = Field(gt=0)
    url: HttpUrl
    location: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=160)
    checked_at: datetime
    segment: str | None = Field(default=None, max_length=80)
    segment_code: str = "UNKNOWN"
    market_position: str = "unknown"
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class ClassifiedListing(BaseModel):
    listing: CarListing
    price_difference: int
    price_difference_percent: float


class CompetitorGroups(BaseModel):
    direct: list[ClassifiedListing] = Field(default_factory=list)
    expensive: list[ClassifiedListing] = Field(default_factory=list)
    cheaper: list[ClassifiedListing] = Field(default_factory=list)


class ModelGroup(BaseModel):
    brand: str
    model: str
    listings_count: int = Field(ge=1)
    average_price: int = Field(gt=0)
    min_price: int = Field(gt=0)
    max_price: int = Field(gt=0)


class CategoryResult(BaseModel):
    listings: list[ClassifiedListing] = Field(default_factory=list)
    model_groups: list[ModelGroup] = Field(default_factory=list)


class SourceDiagnostics(BaseModel):
    pages_scanned: int = Field(default=0, ge=0)
    raw_count: int = Field(default=0, ge=0)
    parsed_count: int = Field(default=0, ge=0)
    accepted_count: int = Field(default=0, ge=0)
    detail_requests: int = Field(default=0, ge=0)
    browser_fallbacks: int = Field(default=0, ge=0)
    http_requests: int = Field(default=0, ge=0)
    routes_attempted: int = Field(default=0, ge=0)
    partial_failures: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    rejected: dict[str, int] = Field(default_factory=dict)
    resolved_url: str | None = None
    resolved_urls: list[str] = Field(default_factory=list)
    degraded: bool = False
    notes: list[str] = Field(default_factory=list)
