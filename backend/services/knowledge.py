from datetime import UTC, datetime, timedelta

from backend.config import Settings, get_settings
from backend.database.knowledge import KnowledgeRepository
from backend.models.car import BodyType
from backend.models.knowledge import CarKnowledgeResult, KnowledgeState


class CarKnowledgeService:
    def __init__(
        self,
        repository: KnowledgeRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.repository = repository or KnowledgeRepository()
        self.settings = settings or get_settings()

    def get(
        self,
        brand: str,
        model: str,
        year: int,
        body_type: BodyType | None = None,
        segment: str | None = None,
        generation: str = "",
    ) -> CarKnowledgeResult:
        profile = self.repository.get_profile(brand, model, year, generation)
        if profile is None:
            return CarKnowledgeResult(
                status=KnowledgeState.UNKNOWN_VEHICLE,
                profile=None,
                problems=None,
            )
        problems = self.repository.get_problem_profile(profile.id)
        if problems is not None and self._fresh(problems.updated_at):
            return CarKnowledgeResult(
                status=KnowledgeState.CACHED,
                profile=profile,
                problems=problems,
            )
        return CarKnowledgeResult(
            status=KnowledgeState.AI_PROVIDER_NOT_CONFIGURED,
            profile=profile,
            problems=problems,
        )

    def _fresh(self, updated_at: datetime) -> bool:
        value = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - value <= timedelta(days=self.settings.car_knowledge_ttl_days)
