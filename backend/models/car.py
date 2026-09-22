from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BodyType(StrEnum):
    SEDAN = "sedan"
    WAGON = "wagon"
    HATCHBACK = "hatchback"
    LIFTBACK = "liftback"
    COUPE = "coupe"
    CONVERTIBLE = "convertible"
    SUV = "suv"
    CROSSOVER = "crossover"
    PICKUP = "pickup"
    MINIVAN = "minivan"
    VAN = "van"


class BodyFilter(StrEnum):
    ANY = "any"
    SEDAN = "sedan"
    WAGON = "wagon"
    HATCHBACK = "hatchback"
    LIFTBACK = "liftback"
    COUPE = "coupe"
    CONVERTIBLE = "convertible"
    SUV = "suv"
    CROSSOVER = "crossover"
    PICKUP = "pickup"
    MINIVAN = "minivan"
    VAN = "van"


class Transmission(StrEnum):
    ANY = "any"
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    ROBOT = "robot"
    CVT = "cvt"


class SearchRegion(StrEnum):
    ANY = "any"
    MOSCOW = "moscow"
    MOSCOW_OBLAST = "moscow_oblast"
    MOSCOW_AND_OBLAST = "moscow_and_oblast"


class Car(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    brand: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    modification: str | None = Field(default=None, max_length=180)
    year: int = Field(ge=1900)
    body_type: BodyType
    transmission: Transmission = Transmission.ANY
    region: SearchRegion = SearchRegion.ANY
    price: int = Field(gt=0)
    segment: str | None = Field(default=None, max_length=80)

    @field_validator("brand", "model")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("year")
    @classmethod
    def validate_year(cls, value: int) -> int:
        if value > datetime.now(UTC).year + 1:
            raise ValueError("year must not exceed next calendar year")
        return value


class SearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    brand: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    modification: str | None = Field(default=None, max_length=300)
    year: int = Field(ge=1900)
    body_type: BodyFilter = BodyFilter.ANY
    transmission: Transmission = Transmission.ANY
    region: SearchRegion = SearchRegion.ANY
    price: int = Field(gt=0)
    generation_id: str | None = Field(default=None, max_length=160)
    modification_id: str | None = Field(default=None, max_length=160)

    @field_validator("brand", "model")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("year")
    @classmethod
    def validate_year(cls, value: int) -> int:
        if value > datetime.now(UTC).year + 1:
            raise ValueError("year must not exceed next calendar year")
        return value


class SourceVehicle(SearchRequest):
    segment: str | None = Field(default=None, max_length=80)
    generation: str | None = Field(default=None, max_length=160)


class MarketDiscoveryRequest(BaseModel):
    """Marketplace-wide constraints; deliberately contains no required make/model."""

    source_vehicle: SearchRequest
    price_from: int = Field(gt=0)
    price_to: int = Field(gt=0)
    body_types: frozenset[BodyType] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_price_window(self) -> "MarketDiscoveryRequest":
        if self.price_from > self.price_to:
            raise ValueError("price_from must not exceed price_to")
        return self


class PriceRanges(BaseModel):
    direct_min: int
    direct_max: int
    expensive_min: int
    expensive_max: int
    cheaper_min: int
    cheaper_max: int
