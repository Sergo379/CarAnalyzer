from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class Car(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    brand: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    year: int = Field(ge=1886, le=2100)
    body_type: BodyType
    price: int = Field(gt=0)
    segment: str | None = Field(default=None, max_length=80)

    @field_validator("brand", "model")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class SearchRequest(Car):
    segment: None = None


class PriceRanges(BaseModel):
    direct_min: int
    direct_max: int
    expensive_min: int
    expensive_max: int
    cheaper_min: int
    cheaper_max: int
