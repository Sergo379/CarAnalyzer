import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from backend.database.db import connect
from backend.models.car import BodyType
from backend.models.knowledge import CarProblemProfile, CarProfile


class KnowledgeRepository:
    def __init__(
        self,
        path: Path | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.path = path
        self.connection = connection
        if connection is not None:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self.connection is not None:
            yield self.connection
            self.connection.commit()
            return
        with connect(self.path) as connection:
            yield connection

    def get_profile(self, brand: str, model: str, year: int) -> CarProfile | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM car_profiles "
                "WHERE lower(brand) = lower(?) AND lower(model) = lower(?) AND year = ? "
                "ORDER BY id LIMIT 1",
                (brand, model, year),
            ).fetchone()
        return self._profile(row) if row else None

    def upsert_profile(
        self,
        brand: str,
        model: str,
        year: int,
        segment: str | None,
        body_types: list[BodyType],
        generation: str = "",
        technical_summary: str | None = None,
        knowledge_updated_at: datetime | None = None,
    ) -> CarProfile:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO car_profiles (
                    brand, model, year, generation, segment, supported_body_types,
                    technical_summary, knowledge_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(brand, model, year, generation) DO UPDATE SET
                    segment = excluded.segment,
                    supported_body_types = excluded.supported_body_types,
                    technical_summary = COALESCE(excluded.technical_summary, technical_summary),
                    knowledge_updated_at = COALESCE(
                        excluded.knowledge_updated_at, knowledge_updated_at
                    )
                """,
                (
                    brand,
                    model,
                    year,
                    generation,
                    segment,
                    json.dumps([body.value for body in body_types]),
                    technical_summary,
                    knowledge_updated_at.isoformat() if knowledge_updated_at else None,
                ),
            )
        profile = self.get_profile(brand, model, year)
        if profile is None:
            raise RuntimeError("Failed to upsert car profile")
        return profile

    def get_problem_profile(self, car_profile_id: int) -> CarProblemProfile | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM car_problem_profiles WHERE car_profile_id = ?",
                (car_profile_id,),
            ).fetchone()
        if row is None:
            return None
        return CarProblemProfile(
            common_problems=json.loads(row["common_problems"]),
            problematic_components=json.loads(row["problematic_components"]),
            inspection_points=json.loads(row["inspection_points"]),
            expensive_failures=json.loads(row["expensive_failures"]),
            risk_summary=row["risk_summary"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_problem_profile(
        self,
        car_profile_id: int,
        profile: CarProblemProfile,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO car_problem_profiles (
                    car_profile_id, common_problems, problematic_components,
                    inspection_points, expensive_failures, risk_summary, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(car_profile_id) DO UPDATE SET
                    common_problems = excluded.common_problems,
                    problematic_components = excluded.problematic_components,
                    inspection_points = excluded.inspection_points,
                    expensive_failures = excluded.expensive_failures,
                    risk_summary = excluded.risk_summary,
                    updated_at = excluded.updated_at
                """,
                (
                    car_profile_id,
                    json.dumps(profile.common_problems, ensure_ascii=False),
                    json.dumps(profile.problematic_components, ensure_ascii=False),
                    json.dumps(profile.inspection_points, ensure_ascii=False),
                    json.dumps(profile.expensive_failures, ensure_ascii=False),
                    profile.risk_summary,
                    profile.updated_at.isoformat(),
                ),
            )

    @staticmethod
    def _profile(row: sqlite3.Row) -> CarProfile:
        return CarProfile(
            id=row["id"],
            brand=row["brand"],
            model=row["model"],
            year=row["year"],
            generation=row["generation"],
            segment=row["segment"],
            supported_body_types=json.loads(row["supported_body_types"]),
            technical_summary=row["technical_summary"],
            knowledge_updated_at=(
                datetime.fromisoformat(row["knowledge_updated_at"])
                if row["knowledge_updated_at"]
                else None
            ),
        )
