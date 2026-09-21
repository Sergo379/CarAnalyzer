from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from backend.models.car import BodyType


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
    year: int = Field(ge=1886, le=2100)
    body_type: BodyType
    price: int = Field(gt=0)
    url: HttpUrl
    location: str | None = Field(default=None, max_length=255)
    checked_at: datetime
    segment: str | None = Field(default=None, max_length=80)


class ClassifiedListing(BaseModel):
    listing: CarListing
    price_difference: int
    price_difference_percent: float


class CompetitorGroups(BaseModel):
    direct: list[ClassifiedListing] = Field(default_factory=list)
    expensive: list[ClassifiedListing] = Field(default_factory=list)
    cheaper: list[ClassifiedListing] = Field(default_factory=list)
