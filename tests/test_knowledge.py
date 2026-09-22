import sqlite3
from datetime import UTC, datetime, timedelta

from backend.config import Settings
from backend.database.init_db import initialize_connection
from backend.database.knowledge import KnowledgeRepository
from backend.models.car import BodyType
from backend.models.knowledge import CarProblemProfile, KnowledgeState
from backend.services.knowledge import CarKnowledgeService


def service_with_database() -> tuple[CarKnowledgeService, sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    initialize_connection(connection)
    repository = KnowledgeRepository(connection=connection)
    service = CarKnowledgeService(
        repository=repository,
        settings=Settings(_env_file=None, car_knowledge_ttl_days=30),
    )
    return service, connection


def test_knowledge_cache_miss_does_not_create_empty_profile() -> None:
    service, connection = service_with_database()
    try:
        result = service.get("BMW", "5 Series", 2022, BodyType.SEDAN, "passenger_premium")
        assert result.status == KnowledgeState.UNKNOWN_VEHICLE
        assert result.profile is None
        assert result.problems is None
        assert connection.execute("SELECT count(*) FROM car_profiles").fetchone()[0] == 0
    finally:
        connection.close()


def test_knowledge_cache_hit_returns_grounded_saved_profile() -> None:
    service, connection = service_with_database()
    try:
        profile = service.repository.upsert_profile("BMW", "520i", 2022, None, [])
        service.repository.save_problem_profile(
            profile.id,
            CarProblemProfile(
                common_problems=["verified problem"],
                risk_summary="verified risk",
                updated_at=datetime.now(UTC),
            ),
        )
        result = service.get("BMW", "520i", 2022)
        assert result.status == KnowledgeState.CACHED
        assert result.problems is not None
        assert result.problems.common_problems == ["verified problem"]
    finally:
        connection.close()


def test_expired_knowledge_is_not_reported_as_fresh() -> None:
    service, connection = service_with_database()
    try:
        profile = service.repository.upsert_profile("BMW", "520i", 2022, None, [])
        service.repository.save_problem_profile(
            profile.id,
            CarProblemProfile(
                common_problems=["old problem"],
                updated_at=datetime.now(UTC) - timedelta(days=31),
            ),
        )
        result = service.get("BMW", "520i", 2022)
        assert result.status == KnowledgeState.AI_PROVIDER_NOT_CONFIGURED
        assert result.problems is not None
    finally:
        connection.close()
