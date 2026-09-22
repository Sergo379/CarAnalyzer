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
    modification: str | None = Field(default=None, max_length=300)
    year: int = Field(ge=1886, le=2100)
    body_type: BodyType
    transmission: Transmission | None = None
    price: int = Field(gt=0)
    url: HttpUrl
    location: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=160)
    checked_at: datetime
    segment: str | None = Field(default=None, max_length=80)
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
