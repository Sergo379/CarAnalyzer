"""Permanent canonical profile cache and claim→chunk evidence graph."""

import sqlite3
from datetime import UTC, datetime

from backend.knowledge.identity import KnowledgeIdentity
from backend.knowledge.profile import TechnicalProfile


class ProfileRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(
        self, identity: KnowledgeIdentity, *, allow_fallback: bool = True
    ) -> tuple[TechnicalProfile, KnowledgeIdentity] | None:
        candidates = identity.fallback() if allow_fallback else (identity,)
        insufficient: tuple[TechnicalProfile, KnowledgeIdentity] | None = None
        for scope in candidates:
            for key in scope.storage_scope_keys():
                row = self.connection.execute(
                    "SELECT result_json FROM problem_profiles WHERE scope_key=?", (key,)
                ).fetchone()
                if not row:
                    continue
                profile = TechnicalProfile.model_validate_json(row[0])
                if profile.status == "complete":
                    return profile, scope
                if insufficient is None:
                    insufficient = profile, scope
        return insufficient

    def save(
        self,
        identity: KnowledgeIdentity,
        profile: TechnicalProfile,
        *,
        pipeline_version: str,
        embedding_model: str,
        llm_model: str,
        overwrite: bool = False,
    ) -> tuple[TechnicalProfile, bool]:
        cached = self.get(identity, allow_fallback=False)
        if cached and not overwrite:
            return cached[0], False
        with self.connection:
            if cached:
                keys = identity.storage_scope_keys()
                marks = ",".join("?" for _ in keys)
                profile_id = int(
                    self.connection.execute(
                        f"SELECT id FROM problem_profiles WHERE scope_key IN ({marks}) "
                        "ORDER BY (scope_key=?) DESC LIMIT 1",
                        (*keys, identity.scope_key),
                    ).fetchone()[0]
                )
                self.connection.execute(
                    "DELETE FROM claim_evidence WHERE claim_id IN "
                    "(SELECT id FROM problem_claims WHERE profile_id=?)",
                    (profile_id,),
                )
                self.connection.execute(
                    "DELETE FROM problem_claims WHERE profile_id=?", (profile_id,)
                )
                self.connection.execute(
                    """UPDATE problem_profiles SET scope_key=?, status=?, result_json=?,
                       created_at=?,
                       pipeline_version=?, embedding_model=?, llm_model=?, source_count=?,
                       evidence_count=?, confidence=? WHERE id=?""",
                    (
                        identity.scope_key,
                        profile.status,
                        profile.model_dump_json(),
                        datetime.now(UTC).isoformat(),
                        pipeline_version,
                        embedding_model,
                        llm_model,
                        profile.source_count,
                        profile.evidence_count,
                        profile.confidence,
                        profile_id,
                    ),
                )
            else:
                cursor = self.connection.execute(
                    """INSERT INTO problem_profiles (
                    scope_key, canonical_brand_id, canonical_model_id,
                    canonical_generation_id, canonical_engine_id, canonical_modification_id,
                    status, result_json, created_at, pipeline_version, embedding_model,
                    llm_model, source_count, evidence_count, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        identity.scope_key,
                        identity.canonical_brand_id,
                        identity.canonical_model_id,
                        identity.canonical_generation_id,
                        identity.canonical_engine_id,
                        identity.canonical_modification_id,
                        profile.status,
                        profile.model_dump_json(),
                        datetime.now(UTC).isoformat(),
                        pipeline_version,
                        embedding_model,
                        llm_model,
                        profile.source_count,
                        profile.evidence_count,
                        profile.confidence,
                    ),
                )
                profile_id = int(cursor.lastrowid)
            for category in (
                "common_problems",
                "problematic_components",
                "inspection_points",
                "expensive_failures",
            ):
                for claim in getattr(profile, category):
                    claim_cursor = self.connection.execute(
                        """INSERT INTO problem_claims
                         (profile_id, category, component, title, description,
                          scope_key, confidence)
                         VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            profile_id,
                            category,
                            claim.component,
                            claim.title,
                            claim.description,
                            claim.scope_key,
                            claim.confidence,
                        ),
                    )
                    self.connection.executemany(
                        "INSERT INTO claim_evidence(claim_id, chunk_id) VALUES (?, ?)",
                        [
                            (int(claim_cursor.lastrowid), chunk_id)
                            for chunk_id in claim.evidence_refs
                        ],
                    )
        return profile, True

    def sources(self, identity: KnowledgeIdentity) -> list[dict[str, object]]:
        scopes = [key for scope in identity.fallback() for key in scope.storage_scope_keys()]
        marks = ",".join("?" for _ in scopes)
        rows = self.connection.execute(
            f"""SELECT s.url, s.title, s.domain, s.source_type, s.status,
                       d.content_hash, d.language, GROUP_CONCAT(c.id),
                       s.discovery_provider, s.discovery_query, s.error_type,
                       count(DISTINCT c.id)
                FROM knowledge_sources s
                LEFT JOIN knowledge_source_documents sd ON sd.source_id=s.id
                LEFT JOIN knowledge_documents d ON d.id=sd.document_id
                LEFT JOIN knowledge_chunks c ON c.document_id=d.id
                WHERE s.scope_key IN ({marks}) AND s.corpus_kind='production'
                GROUP BY s.id ORDER BY s.domain, s.url""",
            scopes,
        ).fetchall()
        return [
            {
                "url": row[0],
                "title": row[1],
                "domain": row[2],
                "source_type": row[3],
                "status": row[4],
                "content_hash": row[5],
                "language": row[6],
                "chunk_ids": [int(value) for value in row[7].split(",")] if row[7] else [],
                "discovery_provider": row[8],
                "generated_query": row[9],
                "failure_reason": row[10],
                "chunk_count": int(row[11]),
            }
            for row in rows
        ]
