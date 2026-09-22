from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from backend.models.car import BodyType


class KnowledgeState(StrEnum):
    CACHED = "cached"
    AI_PROVIDER_NOT_CONFIGURED = "ai_provider_not_configured"
    UNKNOWN_VEHICLE = "unknown_vehicle"


class CarProfile(BaseModel):
    id: int
    brand: str
    model: str
    year: int
    generation: str = ""
    segment: str | None = None
    supported_body_types: list[BodyType] = Field(default_factory=list)
    technical_summary: str | None = None
    knowledge_updated_at: datetime | None = None


class CarProblemProfile(BaseModel):
    common_problems: list[str] = Field(default_factory=list)
    problematic_components: list[str] = Field(default_factory=list)
    inspection_points: list[str] = Field(default_factory=list)
    expensive_failures: list[str] = Field(default_factory=list)
    risk_summary: str = ""
    updated_at: datetime


class CarKnowledgeResult(BaseModel):
    status: KnowledgeState
    profile: CarProfile | None = None
    problems: CarProblemProfile | None = None
