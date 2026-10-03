"""Scope-safe FTS5 + sqlite-vec hybrid retrieval."""

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from backend.config import PROJECT_ROOT
from backend.knowledge.grabber import evidence_scope_identity
from backend.knowledge.identity import KnowledgeIdentity
from backend.knowledge.vector_store import SqliteVectorStore, _blob

_TOKEN = re.compile(r"[\wа-яё]{2,}", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    chunk_id: int
    content: str
    section: str
    component: str
    scope_key: str
    source_urls: tuple[str, ...]
    score: float
    content_hash: str
    document_id: int = 0
    source_ids: tuple[int, ...] = ()
    source_types: tuple[str, ...] = ()
    corpus_kind: str = ""
    evidence_tier: str = ""


class Reranker(Protocol):
    def rerank(self, query: str, hits: list[KnowledgeHit], limit: int) -> list[KnowledgeHit]: ...


class LocalCrossEncoderReranker:
    model_name = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    model_version = "default"

    def __init__(self) -> None:
        self._model = None

    def rerank(self, query: str, hits: list[KnowledgeHit], limit: int) -> list[KnowledgeHit]:
        if not hits or limit <= 0:
            return []
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise RuntimeError("Install the 'knowledge' extra for local reranking") from exc
        if self._model is None:
            self._model = CrossEncoder(
                self.model_name, cache_folder=str(PROJECT_ROOT / "data" / "model_cache")
            )
        scores = self._model.predict([(query, hit.content) for hit in hits])
        ranked = [replace(hit, score=float(score)) for hit, score in zip(hits, scores, strict=True)]
        return sorted(ranked, key=lambda hit: (-hit.score, hit.chunk_id))[:limit]


@dataclass(frozen=True, slots=True)
class RetrievalDiagnostics:
    lexical_candidates: int
    vector_candidates: int
    merged_candidates: int
    filtered_candidates: int
    reranked_candidates: int
    final_context_chunks: int


class HybridRetriever:
    def __init__(
        self,
        connection: sqlite3.Connection,
        vector_store: SqliteVectorStore | None = None,
        reranker: Reranker | None = None,
        corpus_kind: str = "production",
    ) -> None:
        if corpus_kind not in {"production", "evaluation"}:
            raise ValueError("Unsupported corpus kind")
        self.connection = connection
        self.vector_store = vector_store
        self.reranker = reranker
        self.corpus_kind = corpus_kind

    def _allowed(self, identity: KnowledgeIdentity) -> list[str]:
        return [key for item in identity.fallback() for key in item.storage_scope_keys()]

    def _fetch_hits(
        self,
        ids: list[int],
        identity: KnowledgeIdentity,
        component: str | None,
        source_type: str | None,
    ) -> dict[int, KnowledgeHit]:
        if not ids:
            return {}
        scopes = self._allowed(identity)
        id_marks = ",".join("?" for _ in ids)
        scope_marks = ",".join("?" for _ in scopes)
        rows = self.connection.execute(
            f"""SELECT c.id, c.content, c.section, c.component, d.scope_key,
                c.content_hash, s.url, s.title, d.id, s.id, s.source_type
             FROM knowledge_chunks c
             JOIN knowledge_documents d ON d.id=c.document_id
             JOIN knowledge_source_documents sd ON sd.document_id=d.id
             JOIN knowledge_sources s ON s.id=sd.source_id
             WHERE c.id IN ({id_marks}) AND d.scope_key IN ({scope_marks})
               AND (? IS NULL OR c.component='' OR c.component=?)
               AND (? IS NULL OR s.source_type=?) AND s.status='complete'
               AND s.corpus_kind=?
             ORDER BY c.id, s.url""",
            [
                *ids,
                *scopes,
                component,
                component,
                source_type,
                source_type,
                self.corpus_kind,
            ],
        ).fetchall()
        grouped: dict[int, KnowledgeHit] = {}
        for row in rows:
            evidence_identity = evidence_scope_identity(identity, str(row[7]), str(row[1]))
            if evidence_identity is None:
                continue
            effective_scope = KnowledgeIdentity(*json.loads(str(row[4]))).scope_key
            if effective_scope == identity.scope_key:
                effective_scope = evidence_identity.scope_key
            chunk_id = int(row[0])
            if chunk_id in grouped:
                old = grouped[chunk_id]
                grouped[chunk_id] = replace(
                    old,
                    source_urls=(*old.source_urls, str(row[6])),
                    source_ids=(*old.source_ids, int(row[9])),
                    source_types=(*old.source_types, str(row[10])),
                )
            else:
                grouped[chunk_id] = KnowledgeHit(
                    chunk_id,
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    effective_scope,
                    (str(row[6]),),
                    0.0,
                    str(row[5]),
                    int(row[8]),
                    (int(row[9]),),
                    (str(row[10]),),
                    self.corpus_kind,
                )
        return grouped

    def lexical(
        self, query: str, identity: KnowledgeIdentity, limit: int = 30
    ) -> list[tuple[int, float]]:
        tokens = [match.group().casefold() for match in _TOKEN.finditer(query)][:12]
        if not tokens or limit <= 0:
            return []
        fts_query = " OR ".join(f'"{token}"' for token in dict.fromkeys(tokens))
        scopes = self._allowed(identity)
        marks = ",".join("?" for _ in scopes)
        rows = self.connection.execute(
            f"""SELECT c.id, bm25(knowledge_chunks_fts)
             FROM knowledge_chunks_fts
             JOIN knowledge_chunks c ON c.id=knowledge_chunks_fts.rowid
             JOIN knowledge_documents d ON d.id=c.document_id
             JOIN knowledge_source_documents sd ON sd.document_id=d.id
             JOIN knowledge_sources s ON s.id=sd.source_id
             WHERE knowledge_chunks_fts MATCH ? AND d.scope_key IN ({marks})
               AND s.status='complete' AND s.corpus_kind=?
             ORDER BY bm25(knowledge_chunks_fts) LIMIT ?""",
            (fts_query, *scopes, self.corpus_kind, limit),
        ).fetchall()
        return [(int(row[0]), float(row[1])) for row in rows]

    def vector(
        self,
        query_vector: list[float],
        model: str,
        version: str,
        identity: KnowledgeIdentity,
        limit: int = 30,
    ) -> list[tuple[int, float]]:
        if self.vector_store is None or limit <= 0:
            return []
        table = self.vector_store._table(model, version, len(query_vector))
        if not self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
        ).fetchone():
            return []
        scopes = self._allowed(identity)
        marks = ",".join("?" for _ in scopes)
        rows = self.connection.execute(
            f"""SELECT c.id, vec_distance_l2(v.embedding, ?)
             FROM {table} v
             JOIN knowledge_chunks c ON c.id=v.rowid
             JOIN knowledge_documents d ON d.id=c.document_id
             JOIN knowledge_source_documents sd ON sd.document_id=d.id
             JOIN knowledge_sources s ON s.id=sd.source_id
             WHERE d.scope_key IN ({marks})
               AND s.status='complete' AND s.corpus_kind=?
             ORDER BY vec_distance_l2(v.embedding, ?) LIMIT ?""",
            (_blob(query_vector), *scopes, self.corpus_kind, _blob(query_vector), limit),
        ).fetchall()
        return [(int(row[0]), float(row[1])) for row in rows]

    def retrieve(
        self,
        query: str,
        identity: KnowledgeIdentity,
        *,
        query_vector: list[float] | None = None,
        embedding_model: str = "",
        embedding_version: str = "default",
        top_k: int = 5,
        candidate_k: int = 30,
        lexical_weight: float = 0.5,
        component: str | None = None,
        source_type: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[list[KnowledgeHit], RetrievalDiagnostics]:
        if not 0 <= lexical_weight <= 1:
            raise ValueError("Hybrid weight must be between 0 and 1")
        lexical = self.lexical(query, identity, candidate_k)
        if progress:
            progress("retrieving_vectors")
        vector = (
            self.vector(query_vector, embedding_model, embedding_version, identity, candidate_k)
            if query_vector is not None
            else []
        )
        scores: dict[int, float] = {}
        for rank, (chunk_id, _) in enumerate(lexical, 1):
            scores[chunk_id] = scores.get(chunk_id, 0) + lexical_weight / (60 + rank)
        for rank, (chunk_id, _) in enumerate(vector, 1):
            scores[chunk_id] = scores.get(chunk_id, 0) + (1 - lexical_weight) / (60 + rank)
        ranked_ids = sorted(scores, key=lambda key: (-scores[key], key))
        fetched = self._fetch_hits(ranked_ids, identity, component, source_type)
        dedup: list[KnowledgeHit] = []
        seen_hashes: set[str] = set()
        for chunk_id in ranked_ids:
            hit = fetched.get(chunk_id)
            if not hit or hit.content_hash in seen_hashes:
                continue
            seen_hashes.add(hit.content_hash)
            dedup.append(replace(hit, score=scores[chunk_id]))
        if progress and self.reranker:
            progress("reranking")
        reranked = (
            self.reranker.rerank(query, dedup[:candidate_k], top_k)
            if self.reranker
            else dedup[:top_k]
        )
        return reranked, RetrievalDiagnostics(
            len(lexical),
            len(vector),
            len(scores),
            len(dedup),
            len(reranked) if self.reranker else 0,
            len(reranked),
        )
