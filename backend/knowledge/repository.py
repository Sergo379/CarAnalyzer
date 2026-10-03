"""Idempotent technical-source and document persistence."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from backend.knowledge.grabber import DiscoveryAttempt
from backend.knowledge.identity import KnowledgeIdentity
from backend.knowledge.processing import CleanDocument, KnowledgeChunk
from backend.knowledge.progress import runtime_timing


@dataclass(frozen=True, slots=True)
class DocumentSaveResult:
    source_id: int
    document_id: int
    document_created: bool
    chunks_created: int


class KnowledgeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save_document(
        self,
        identity: KnowledgeIdentity,
        url: str,
        source_type: str,
        document: CleanDocument,
        chunks: list[KnowledgeChunk],
        fetched_at: datetime | None = None,
        corpus_kind: str = "production",
        discovery_provider: str = "",
        discovery_query: str = "",
    ) -> DocumentSaveResult:
        if not document.content:
            raise ValueError("Empty document cannot be ingested")
        if corpus_kind not in {"production", "evaluation"}:
            raise ValueError("Unsupported corpus kind")
        now = (fetched_at or datetime.now(UTC)).isoformat()
        domain = urlsplit(url).hostname or ""
        if not domain:
            raise ValueError("Source URL must have a domain")
        self.connection.execute(
            """INSERT INTO knowledge_sources (
             scope_key, canonical_brand_id, canonical_model_id, canonical_generation_id,
             canonical_engine_id, canonical_modification_id, domain, source_type, corpus_kind,
             discovery_provider, discovery_query, url,
             title, discovered_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
            ON CONFLICT(scope_key, url) DO UPDATE SET
             title=excluded.title, corpus_kind=excluded.corpus_kind,
             discovery_provider=excluded.discovery_provider,
             discovery_query=excluded.discovery_query,
             status='complete', error_type=NULL""",
            (
                identity.scope_key,
                identity.canonical_brand_id,
                identity.canonical_model_id,
                identity.canonical_generation_id,
                identity.canonical_engine_id,
                identity.canonical_modification_id,
                domain,
                source_type,
                corpus_kind,
                discovery_provider,
                discovery_query,
                url,
                document.title,
                now,
            ),
        )
        source_id = int(
            self.connection.execute(
                "SELECT id FROM knowledge_sources WHERE scope_key=? AND url=?",
                (identity.scope_key, url),
            ).fetchone()[0]
        )
        existing = self.connection.execute(
            "SELECT id FROM knowledge_documents WHERE scope_key=? AND content_hash=?",
            (identity.scope_key, document.content_hash),
        ).fetchone()
        created = existing is None
        if created:
            cursor = self.connection.execute(
                """INSERT INTO knowledge_documents
                 (scope_key, content_hash, content, language, fetched_at)
                 VALUES (?, ?, ?, ?, ?)""",
                (
                    identity.scope_key,
                    document.content_hash,
                    document.content,
                    document.language,
                    now,
                ),
            )
            document_id = int(cursor.lastrowid)
            self.connection.executemany(
                """INSERT INTO knowledge_chunks
                 (document_id, chunk_index, content, content_hash, section, component,
                  language, metadata)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        document_id,
                        chunk.index,
                        chunk.content,
                        chunk.content_hash,
                        chunk.section,
                        chunk.component,
                        document.language,
                        json.dumps(
                            {
                                "scope_key": identity.scope_key,
                                "canonical_brand_id": identity.canonical_brand_id,
                                "canonical_model_id": identity.canonical_model_id,
                                "canonical_generation_id": identity.canonical_generation_id,
                                "canonical_engine_id": identity.canonical_engine_id,
                                "canonical_modification_id": identity.canonical_modification_id,
                                "brand": identity.brand,
                                "model": identity.model,
                                "generation": identity.generation,
                                "engine": identity.engine,
                                "source_type": source_type,
                                "source_url": url,
                                "document_hash": document.content_hash,
                                "section": chunk.section,
                                "component": chunk.component,
                                "language": document.language,
                            },
                            ensure_ascii=False,
                        ),
                    )
                    for chunk in chunks
                ],
            )
        else:
            document_id = int(existing[0])
        self.connection.execute(
            """INSERT INTO knowledge_source_documents(source_id, document_id, linked_at)
             VALUES (?, ?, ?)
             ON CONFLICT(source_id) DO UPDATE SET
              document_id=excluded.document_id, linked_at=excluded.linked_at""",
            (source_id, document_id, now),
        )
        self.connection.commit()
        return DocumentSaveResult(source_id, document_id, created, len(chunks) if created else 0)

    def source_count(self, scope_key: str) -> int:
        return int(
            self.connection.execute(
                """SELECT count(*) FROM knowledge_sources
                   WHERE scope_key=? AND status='complete' AND corpus_kind='production'""",
                (scope_key,),
            ).fetchone()[0]
        )

    def record_source_status(
        self,
        identity: KnowledgeIdentity,
        url: str,
        title: str,
        source_type: str,
        status: str,
        error_type: str | None = None,
        discovery_provider: str = "",
        discovery_query: str = "",
    ) -> None:
        domain = urlsplit(url).hostname or ""
        self.connection.execute(
            """INSERT INTO knowledge_sources (
                scope_key, canonical_brand_id, canonical_model_id,
                canonical_generation_id, canonical_engine_id, canonical_modification_id,
                domain, source_type, discovery_provider, discovery_query,
                url, title, discovered_at, status, error_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key, url) DO UPDATE SET
                status=excluded.status, error_type=excluded.error_type,
                discovery_provider=excluded.discovery_provider,
                discovery_query=excluded.discovery_query""",
            (
                identity.scope_key,
                identity.canonical_brand_id,
                identity.canonical_model_id,
                identity.canonical_generation_id,
                identity.canonical_engine_id,
                identity.canonical_modification_id,
                domain,
                source_type,
                discovery_provider,
                discovery_query,
                url,
                title,
                datetime.now(UTC).isoformat(),
                status,
                error_type,
            ),
        )
        self.connection.commit()

    def record_discovery_attempts(
        self,
        identity: KnowledgeIdentity,
        build_id: str,
        attempts: list[DiscoveryAttempt],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connection:
            self.connection.executemany(
                """INSERT INTO knowledge_discovery_attempts
                   (build_id, scope_key, provider, query, status, result_count,
                    error_type, error_message, created_at, language)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(build_id, provider, query, language) DO UPDATE SET
                    status=excluded.status, result_count=excluded.result_count,
                    error_type=excluded.error_type, error_message=excluded.error_message""",
                [
                    (
                        build_id,
                        identity.scope_key,
                        attempt.provider,
                        attempt.query,
                        attempt.status,
                        attempt.result_count,
                        attempt.error_type,
                        attempt.error_message,
                        now,
                        attempt.language,
                    )
                    for attempt in attempts
                ],
            )

    def discovery_attempts(self, identity: KnowledgeIdentity) -> list[dict[str, object]]:
        scopes = [key for scope in identity.fallback() for key in scope.storage_scope_keys()]
        marks = ",".join("?" for _ in scopes)
        rows = self.connection.execute(
            f"""SELECT build_id, provider, query, status, result_count,
                       error_type, error_message, created_at, language
                FROM knowledge_discovery_attempts
                WHERE scope_key IN ({marks})
                ORDER BY id DESC LIMIT 100""",
            scopes,
        ).fetchall()
        return [
            {
                "build_id": row[0],
                "provider": row[1],
                "query": row[2],
                "status": row[3],
                "result_count": row[4],
                "error_type": row[5],
                "error_message": row[6],
                "created_at": row[7],
                "language": row[8],
            }
            for row in rows
        ]

    def start_build(self, build_id: str, identity: KnowledgeIdentity, started_at: str) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO knowledge_builds
                   (build_id, scope_key, status, phase, started_at)
                   VALUES (?, ?, 'building', 'queued', ?)""",
                (build_id, identity.scope_key, started_at),
            )

    def update_build(
        self,
        build_id: str,
        *,
        status: str,
        phase: str,
        diagnostics: dict[str, object],
        finished_at: str | None = None,
        last_error: str = "",
        error_type: str = "",
        profile_persisted: bool = False,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE knowledge_builds SET
                     status=?, phase=?, diagnostics_json=?, finished_at=?,
                     last_error=?, error_type=?, profile_persisted=?
                   WHERE build_id=?""",
                (
                    status,
                    phase,
                    json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":")),
                    finished_at,
                    last_error,
                    error_type,
                    int(profile_persisted),
                    build_id,
                ),
            )

    def latest_build(self, identity: KnowledgeIdentity) -> dict[str, object] | None:
        scopes = identity.storage_scope_keys()
        marks = ",".join("?" for _ in scopes)
        row = self.connection.execute(
            f"""SELECT build_id, scope_key, status, phase, started_at, finished_at,
                      diagnostics_json, last_error, error_type, profile_persisted
               FROM knowledge_builds WHERE scope_key IN ({marks})
               ORDER BY started_at DESC LIMIT 1""",
            scopes,
        ).fetchone()
        if row is None:
            return None
        diagnostics = json.loads(row[6] or "{}")
        return runtime_timing(
            {
                "build_id": row[0],
                "scope_key": identity.scope_key,
                "status": row[2],
                "phase": row[3],
                "started_at": row[4],
                "finished_at": row[5],
                "diagnostics": diagnostics,
                **diagnostics,
                "last_error": row[7],
                "error_type": row[8],
                "profile_persisted": bool(row[9]),
            }
        )

    def document_ids(self, identity: KnowledgeIdentity, *, exact_scope: bool = False) -> list[int]:
        scopes = (
            list(identity.storage_scope_keys())
            if exact_scope
            else [key for scope in identity.fallback() for key in scope.storage_scope_keys()]
        )
        marks = ",".join("?" for _ in scopes)
        return [
            int(row[0])
            for row in self.connection.execute(
                f"""SELECT DISTINCT d.id FROM knowledge_documents d
                 JOIN knowledge_source_documents sd ON sd.document_id=d.id
                 JOIN knowledge_sources s ON s.id=sd.source_id
                 WHERE d.scope_key IN ({marks}) AND s.status='complete'
                   AND s.corpus_kind='production'""",
                scopes,
            ).fetchall()
        ]
