from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.models.car import BodyType, Transmission


class SourceReference(BaseModel):
    source: str
    url: str
    path: str
    slug: str


class EngineSpec(BaseModel):
    fuel_type: str | None = None
    displacement_l: float | None = Field(default=None, gt=0)
    power_hp: int | None = Field(default=None, gt=0)
    engine_code: str | None = None


class VehicleModification(BaseModel):
    id: str
    name: str
    engine: EngineSpec
    transmission: Transmission | None = None
    drivetrain: str | None = None
    body_type: BodyType | None = None
    source_refs: list[SourceReference] = Field(default_factory=list)

    @property
    def label(self) -> str:
        parts = [self.name]
        engine = self.engine
        specification = []
        if engine.displacement_l is not None:
            specification.append(f"{engine.displacement_l:g} л")
        if engine.fuel_type:
            specification.append(engine.fuel_type)
        if engine.power_hp is not None:
            specification.append(f"{engine.power_hp} л.с.")
        if specification:
            parts.append(" · ".join(specification))
        return " · ".join(parts)


class VehicleGeneration(BaseModel):
    id: str
    name: str
    year_from: int | None = None
    year_to: int | None = None
    body_types: list[BodyType] = Field(default_factory=list)
    source_refs: list[SourceReference] = Field(default_factory=list)
    modifications: list[VehicleModification] = Field(default_factory=list)


class CatalogModel(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    source_refs: list[SourceReference] = Field(default_factory=list)
    generations: list[VehicleGeneration] = Field(default_factory=list)
    details_updated_at: datetime | None = None
    details_parser_version: int | None = None
    details_status: Literal[
        "complete",
        "source_has_no_generation_data",
        "parse_error",
        "source_unavailable",
        "rate_limited",
        "not_checked",
    ] = "not_checked"
    details_checked_at: datetime | None = None
    details_retry_at: datetime | None = None
    details_error_type: str | None = None
    details_error_detail: str | None = None


class CatalogBrand(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    source_refs: list[SourceReference] = Field(default_factory=list)
    models: list[CatalogModel] = Field(default_factory=list)


class CanonicalVehicleIdentity(BaseModel):
    canonical_brand_id: str
    canonical_model_id: str
    brand: str
    model: str
    provisional: bool = False
