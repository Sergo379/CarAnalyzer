"""One-shot technical build; live market endpoints never call this service."""

import asyncio
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from backend.config import get_settings
from backend.database.init_db import initialize_connection
from backend.knowledge.embeddings import EmbeddingProvider, LocalSentenceEmbedding
from backend.knowledge.evidence_policy import select_usable_evidence, source_domains
from backend.knowledge.gemini import GeminiProvider, ProviderGenerationError
from backend.knowledge.grabber import TechnicalGrabber, validate_source_scope
from backend.knowledge.identity import KnowledgeIdentity
from backend.knowledge.indexer import KnowledgeIndexer
from backend.knowledge.llama_ingest import LlamaIndexIngestAdapter
from backend.knowledge.observability import LangfuseObserver
from backend.knowledge.profile import TechnicalProfile, ValidationCounts, validate_grounded_profile
from backend.knowledge.profile_repository import ProfileRepository
from backend.knowledge.repository import KnowledgeRepository
from backend.knowledge.retrieval import HybridRetriever, LocalCrossEncoderReranker
from backend.knowledge.vector_store import SqliteVectorStore

logger = logging.getLogger(__name__)


class ProfileGenerator(Protocol):
    @property
    def configured(self) -> bool: ...

    async def generate_profile(self, identity, hits, prompt_version="grounded-v1"): ...


@dataclass(slots=True)
class BuildDiagnostics:
    build_id: str = ""
    started_at: str = ""
    phase_started_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    provider: str = ""
    provider_attempt: int = 0
    provider_max_attempts: int = 0
    provider_timeout_seconds: float = 0
    provider_started_at: str = ""
    provider_attempt_started_at: str = ""
    retry_started_at: str = ""
    retry_in_seconds: float = 0
    sdk_response_received: bool = False
    structured_validation_error: str = ""
    structured_validation_errors: list[dict[str, str]] = field(default_factory=list)
    knowledge_path: str = ""
    local_documents: int = 0
    local_usable_evidence: int = 0
    discovery_reason: str = ""
    queries_generated: int = 0
    discovery_providers: list[str] = field(default_factory=list)
    discovery_attempts: list[dict[str, object]] = field(default_factory=list)
    source_categories: list[str] = field(default_factory=list)
    source_domains: list[str] = field(default_factory=list)
    sources_discovered: int = 0
    # Unique production source IDs supporting the selected LLM context, including cached corpus.
    sources_used: int = 0
    sources_loaded: int = 0
    sources_failed: int = 0
    source_statuses: dict[str, int] = field(default_factory=dict)
    documents_created: int = 0
    documents_deduplicated: int = 0
    chunks_created: int = 0
    embeddings_created: int = 0
    lexical_candidates: int = 0
    vector_candidates: int = 0
    merged_candidates: int = 0
    filtered_candidates: int = 0
    reranked_candidates: int = 0
    # Retrieval output before evidence policy; not newly ingested chunks or raw candidates.
    retrieved_chunks: int = 0
    # Evidence-policy-approved chunks selected for LLM context (after its size cap).
    usable_evidence_chunks: int = 0
    final_context_chunks: int = 0
    context_source_count: int = 0
    context_evidence_count: int = 0
    hard_rejected_candidates: int = 0
    usable_candidates: int = 0
    high_evidence_items: int = 0
    medium_evidence_items: int = 0
    limited_evidence_items: int = 0
    high_context_chunks: int = 0
    medium_context_chunks: int = 0
    limited_context_chunks: int = 0
    usable_context_chunks: int = 0
    independent_source_domains: int = 0
    claims_generated: int = 0
    claims_rejected_no_evidence: int = 0
    claims_rejected_scope_mismatch: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_latency_ms: int = 0
    retrieved_chunk_ids: list[int] = field(default_factory=list)
    llm_model: str = ""
    llm_provider: str = ""
    prompt_version: str = ""
    structured_validation_status: str = "not_attempted"
    last_error: str = ""
    error_type: str = ""
    elapsed_ms: int = 0


@dataclass(slots=True)
class BuildResult:
    status: str
    profile: TechnicalProfile | None = None
    diagnostics: BuildDiagnostics = field(default_factory=BuildDiagnostics)
    scope_key: str = ""
    build_id: str = ""


ProgressCallback = Callable[[str, BuildDiagnostics], None]


class TechnicalKnowledgeService:
    pipeline_version = "task2-buyout-inspection-v4"

    def __init__(
        self,
        database_path: Path | None = None,
        grabber: TechnicalGrabber | None = None,
        embedder: EmbeddingProvider | None = None,
        generator: ProfileGenerator | None = None,
        ingest_adapter: LlamaIndexIngestAdapter | None = None,
        reranker: LocalCrossEncoderReranker | None = None,
        minimum_rerank_score: float = -1.0,
        observer: LangfuseObserver | None = None,
    ) -> None:
        self.database_path = database_path or get_settings().resolved_database_path()
        self.grabber = grabber or TechnicalGrabber()
        self.embedder = embedder or LocalSentenceEmbedding()
        self.generator = generator or GeminiProvider()
        self.ingest_adapter = ingest_adapter or LlamaIndexIngestAdapter()
        self.reranker = reranker
        self.minimum_rerank_score = minimum_rerank_score
        self.observer = observer or LangfuseObserver()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_connection(connection)
        return connection

    def cached(self, identity: KnowledgeIdentity) -> BuildResult | None:
        connection = self._connect()
        try:
            found = ProfileRepository(connection).get(identity)
            return (
                BuildResult(
                    "cached",
                    found[0],
                    diagnostics=BuildDiagnostics(knowledge_path="profile_cache"),
                    scope_key=found[1].scope_key,
                )
                if found and found[0].status == "complete"
                else None
            )
        finally:
            connection.close()

    def sources(self, identity: KnowledgeIdentity) -> list[dict[str, object]]:
        connection = self._connect()
        try:
            return ProfileRepository(connection).sources(identity)
        finally:
            connection.close()

    def discovery_attempts(self, identity: KnowledgeIdentity) -> list[dict[str, object]]:
        connection = self._connect()
        try:
            return KnowledgeRepository(connection).discovery_attempts(identity)
        finally:
            connection.close()

    def build_status(self, identity: KnowledgeIdentity) -> dict[str, object] | None:
        connection = self._connect()
        try:
            return KnowledgeRepository(connection).latest_build(identity)
        finally:
            connection.close()

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, ProviderGenerationError):
            message = re.sub(r"(?i)(bearer|api[_ -]?key)\s+\S+", r"\1 [redacted]", str(exc))
            return message[:500]
        return type(exc).__name__

    def _persist_build(
        self,
        build_id: str,
        *,
        status: str,
        phase: str,
        diagnostics: BuildDiagnostics,
        finished_at: str | None = None,
        profile_persisted: bool = False,
    ) -> None:
        connection = self._connect()
        try:
            KnowledgeRepository(connection).update_build(
                build_id,
                status=status,
                phase=phase,
                diagnostics=asdict(diagnostics),
                finished_at=finished_at,
                last_error=diagnostics.last_error,
                error_type=diagnostics.error_type,
                profile_persisted=profile_persisted,
            )
        finally:
            connection.close()

    def _profile_exists(self, identity: KnowledgeIdentity) -> bool:
        connection = self._connect()
        try:
            return bool(
                connection.execute(
                    "SELECT 1 FROM problem_profiles WHERE scope_key=?", (identity.scope_key,)
                ).fetchone()
            )
        finally:
            connection.close()

    async def _retrieve_evidence(
        self,
        connection: sqlite3.Connection,
        identity: KnowledgeIdentity,
        documents: list[int],
        diagnostics: BuildDiagnostics,
        notify: Callable[[str], None],
    ) -> list:
        if not documents:
            return []
        vectors = SqliteVectorStore(connection)
        indexer = KnowledgeIndexer(connection, self.embedder, vectors)
        notify("embedding")
        for document_id in documents:
            diagnostics.embeddings_created += await indexer.index_document(document_id)
        notify("indexing")
        query = (
            f"{identity.brand} {identity.model} {identity.generation} {identity.engine} "
            "used car problems symptoms inspection test drive двигатель коробка подвеска"
        )
        query_vector = (await self.embedder.embed([query], query=True))[0]
        retriever = HybridRetriever(connection, vectors, self.reranker)
        notify("retrieving")
        loop = asyncio.get_running_loop()
        with self.observer.span(
            "knowledge-retrieval", input={"query": query, "scope_key": identity.scope_key}
        ) as retrieval_span:
            retrieved, retrieval = await asyncio.to_thread(
                retriever.retrieve,
                query,
                identity,
                query_vector=query_vector,
                embedding_model=self.embedder.model_name,
                embedding_version=self.embedder.model_version,
                top_k=10,
                candidate_k=20,
                progress=lambda phase: loop.call_soon_threadsafe(notify, phase),
            )
            retrieval_span.update(
                output={
                    "chunk_ids": [hit.chunk_id for hit in retrieved],
                    "lexical_candidates": retrieval.lexical_candidates,
                    "vector_candidates": retrieval.vector_candidates,
                }
            )
        diagnostics.lexical_candidates = retrieval.lexical_candidates
        diagnostics.vector_candidates = retrieval.vector_candidates
        diagnostics.merged_candidates = retrieval.merged_candidates
        diagnostics.filtered_candidates = retrieval.filtered_candidates
        diagnostics.reranked_candidates = retrieval.reranked_candidates
        diagnostics.retrieved_chunks = len(retrieved)
        notify("evaluating_evidence")
        selection = select_usable_evidence(
            retrieved,
            identity,
            reranked=bool(self.reranker),
            minimum_relevance_score=self.minimum_rerank_score,
        )
        hits = selection.hits[:6]
        diagnostics.hard_rejected_candidates = selection.hard_rejected
        diagnostics.usable_candidates = len(hits)
        diagnostics.high_context_chunks = sum(hit.evidence_tier == "high" for hit in hits)
        diagnostics.medium_context_chunks = sum(hit.evidence_tier == "medium" for hit in hits)
        diagnostics.limited_context_chunks = sum(hit.evidence_tier == "limited" for hit in hits)
        diagnostics.usable_context_chunks = len(hits)
        diagnostics.independent_source_domains = len(source_domains(hits))
        diagnostics.final_context_chunks = len(hits)
        diagnostics.retrieved_chunk_ids = [hit.chunk_id for hit in hits]
        diagnostics.context_evidence_count = len(hits)
        diagnostics.context_source_count = len({url for hit in hits for url in hit.source_urls})
        diagnostics.sources_used = len({source_id for hit in hits for source_id in hit.source_ids})
        diagnostics.usable_evidence_chunks = len(hits)
        return hits

    async def build(
        self,
        identity: KnowledgeIdentity,
        *,
        force: bool = False,
        refresh_sources: bool = False,
        progress: ProgressCallback | None = None,
    ) -> BuildResult:
        started = perf_counter()
        started_at = datetime.now(UTC).isoformat()
        build_id = uuid4().hex
        diagnostics = BuildDiagnostics(build_id=build_id, started_at=started_at)
        last_phase = ""

        connection = self._connect()
        try:
            KnowledgeRepository(connection).start_build(build_id, identity, started_at)
        finally:
            connection.close()

        def notify(phase: str, *, force_update: bool = False) -> None:
            nonlocal last_phase
            diagnostics.elapsed_ms = int((perf_counter() - started) * 1000)
            now = datetime.now(UTC).isoformat()
            if phase != last_phase:
                diagnostics.phase_started_at = now
                logger.info(
                    "knowledge build=%s phase=%s scope=%s sources_discovered=%s sources_used=%s "
                    "chunks_created=%s retrieved_chunks=%s usable_evidence_chunks=%s attempt=%s/%s",
                    build_id,
                    phase,
                    identity.scope_key,
                    diagnostics.sources_discovered,
                    diagnostics.sources_used,
                    diagnostics.chunks_created,
                    diagnostics.retrieved_chunks,
                    diagnostics.usable_evidence_chunks,
                    diagnostics.provider_attempt,
                    diagnostics.provider_max_attempts,
                )
            if phase != last_phase or force_update:
                diagnostics.updated_at = now
                last_phase = phase
                self._persist_build(
                    build_id,
                    status="building",
                    phase=phase,
                    diagnostics=diagnostics,
                )
            if progress:
                progress(phase, diagnostics)

        notify("queued")
        with self.observer.span(
            "knowledge-build",
            input={"scope_key": identity.scope_key, "pipeline_version": self.pipeline_version},
        ) as observation:
            try:
                result = await self._build(
                    identity,
                    force=force,
                    refresh_sources=refresh_sources,
                    diagnostics=diagnostics,
                    build_id=build_id,
                    notify=notify,
                )
            except Exception as exc:
                diagnostics.error_type = type(exc).__name__
                diagnostics.last_error = self._safe_error(exc)
                notify("failed")
                self._persist_build(
                    build_id,
                    status="failed",
                    phase="failed",
                    diagnostics=diagnostics,
                    finished_at=datetime.now(UTC).isoformat(),
                )
                observation.update(output={"status": "failed", "error_type": type(exc).__name__})
                raise
            result.build_id = build_id
            diagnostics.finished_at = datetime.now(UTC).isoformat()
            notify(result.status)
            self._persist_build(
                build_id,
                status=result.status,
                phase=result.status,
                diagnostics=diagnostics,
                finished_at=datetime.now(UTC).isoformat(),
                profile_persisted=bool(result.profile and self._profile_exists(identity)),
            )
            observation.update(
                output={
                    "status": result.status,
                    "context_chunks": result.diagnostics.final_context_chunks,
                    "retrieved_chunk_ids": result.diagnostics.retrieved_chunk_ids,
                    "llm_model": result.diagnostics.llm_model,
                    "prompt_version": result.diagnostics.prompt_version,
                    "input_tokens": result.diagnostics.input_tokens,
                    "output_tokens": result.diagnostics.output_tokens,
                }
            )
            return result

    async def _build(
        self,
        identity: KnowledgeIdentity,
        *,
        force: bool,
        refresh_sources: bool,
        diagnostics: BuildDiagnostics,
        build_id: str,
        notify: Callable[[str], None],
    ) -> BuildResult:
        connection = self._connect()
        try:
            notify("checking_profile_cache")
            profiles = ProfileRepository(connection)
            existing = profiles.get(identity, allow_fallback=False)
            if existing and existing[0].status == "complete" and not force:
                diagnostics.knowledge_path = "profile_cache"
                return BuildResult("cached", existing[0], diagnostics, identity.scope_key)
            if not self.generator.configured:
                return BuildResult(
                    "provider_not_configured", diagnostics=diagnostics, scope_key=identity.scope_key
                )
            repository = KnowledgeRepository(connection)
            notify("checking_local_corpus")
            documents = repository.document_ids(identity)
            diagnostics.local_documents = len(documents)
            hits = await self._retrieve_evidence(
                connection, identity, documents, diagnostics, notify
            )
            diagnostics.local_usable_evidence = len(hits)
            if hits and not refresh_sources:
                diagnostics.knowledge_path = "local_corpus"
            else:
                diagnostics.discovery_reason = (
                    "explicit_refresh"
                    if refresh_sources
                    else "no_usable_local_evidence"
                    if documents
                    else "no_local_documents"
                )
                diagnostics.knowledge_path = "discovery"
                notify("discovering_sources")
                async with httpx.AsyncClient(
                    timeout=self.grabber.timeout_seconds,
                    headers={"User-Agent": "CarAnalyzer-Knowledge/0.1"},
                ) as client:
                    if hasattr(self.grabber, "discover_with_report"):
                        discovery = await self.grabber.discover_with_report(identity, client)
                        candidates = discovery.candidates
                        repository.record_discovery_attempts(identity, build_id, discovery.attempts)
                        diagnostics.queries_generated = len(
                            {attempt.query for attempt in discovery.attempts if attempt.query}
                        )
                        diagnostics.discovery_providers = sorted(
                            {attempt.provider for attempt in discovery.attempts}
                        )
                        diagnostics.discovery_attempts = [
                            asdict(attempt) for attempt in discovery.attempts
                        ]
                        for attempt in discovery.attempts:
                            if attempt.status not in {"success", "zero_results"}:
                                diagnostics.source_statuses[attempt.status] = (
                                    diagnostics.source_statuses.get(attempt.status, 0) + 1
                                )
                    else:
                        candidates = await self.grabber.discover(identity, client)
                    diagnostics.sources_discovered = len(candidates)
                    diagnostics.source_categories = sorted(
                        {candidate.source_type for candidate in candidates}
                    )
                    diagnostics.source_domains = sorted(
                        {urlsplit(candidate.url).hostname or "" for candidate in candidates}
                    )
                    notify("loading_documents")
                    loaded_documents = []
                    for candidate in candidates:
                        loaded = await self.grabber.load(candidate, client)
                        loaded = validate_source_scope(identity, loaded)
                        if loaded.document is None:
                            failed_candidate = loaded.candidate
                            repository.record_source_status(
                                failed_candidate.identity,
                                failed_candidate.url,
                                failed_candidate.title,
                                failed_candidate.source_type,
                                loaded.status,
                                error_type=loaded.status,
                                discovery_provider=failed_candidate.discovery_provider,
                                discovery_query=failed_candidate.discovery_query,
                            )
                            diagnostics.sources_failed += 1
                            diagnostics.source_statuses[loaded.status] = (
                                diagnostics.source_statuses.get(loaded.status, 0) + 1
                            )
                            notify("loading_documents")
                            continue
                        loaded_documents.append(loaded)
                        diagnostics.sources_loaded += 1
                        notify("loading_documents")
                    notify("processing_documents")
                    notify("chunking")
                    for loaded in loaded_documents:
                        candidate = loaded.candidate
                        assert loaded.document is not None
                        chunks = self.ingest_adapter.chunk(loaded.document, candidate.identity)
                        saved = repository.save_document(
                            candidate.identity,
                            candidate.url,
                            candidate.source_type,
                            loaded.document,
                            chunks,
                            discovery_provider=candidate.discovery_provider,
                            discovery_query=candidate.discovery_query,
                        )
                        diagnostics.documents_created += int(saved.document_created)
                        diagnostics.documents_deduplicated += int(not saved.document_created)
                        diagnostics.chunks_created += saved.chunks_created
                        notify("chunking")
            documents = repository.document_ids(identity)
            if not documents:
                failure = (
                    "search_provider_unavailable"
                    if any(
                        diagnostics.source_statuses.get(status)
                        for status in (
                            "dependency_missing",
                            "provider_unavailable",
                            "network_error",
                        )
                    )
                    else "blocked"
                    if diagnostics.source_statuses.get("blocked")
                    else "timeout"
                    if diagnostics.source_statuses.get("timeout")
                    else "parse_error"
                    if diagnostics.sources_failed
                    else "no_sources"
                )
                if failure == "no_sources":
                    if existing and existing[0].status == "complete":
                        return BuildResult(
                            "rebuild_preserved", existing[0], diagnostics, identity.scope_key
                        )
                    empty = TechnicalProfile(status="insufficient_evidence")
                    profiles.save(
                        identity,
                        empty,
                        pipeline_version=self.pipeline_version,
                        embedding_model=self.embedder.model_name,
                        llm_model="none",
                        overwrite=force or bool(existing),
                    )
                    return BuildResult(failure, empty, diagnostics, identity.scope_key)
                return BuildResult(failure, diagnostics=diagnostics, scope_key=identity.scope_key)
            if diagnostics.knowledge_path == "discovery":
                hits = await self._retrieve_evidence(
                    connection, identity, documents, diagnostics, notify
                )
            if not hits:
                if existing and existing[0].status == "complete":
                    return BuildResult(
                        "rebuild_preserved", existing[0], diagnostics, identity.scope_key
                    )
                empty = TechnicalProfile(status="insufficient_evidence")
                profiles.save(
                    identity,
                    empty,
                    pipeline_version=self.pipeline_version,
                    embedding_model=self.embedder.model_name,
                    llm_model="none",
                    overwrite=force or bool(existing),
                )
                return BuildResult("insufficient_evidence", empty, diagnostics, identity.scope_key)
            notify("calling_gemini")
            generation_started = perf_counter()
            diagnostics.llm_provider = (
                "Gemini" if isinstance(self.generator, GeminiProvider) else "custom"
            )
            diagnostics.llm_model = get_settings().gemini_model
            diagnostics.prompt_version = "buyout-inspection-v4"

            def provider_progress(phase: str, fields: dict[str, object]) -> None:
                for key, value in fields.items():
                    if hasattr(diagnostics, key):
                        setattr(diagnostics, key, value)
                notify(phase, force_update=True)

            try:
                with self.observer.span(
                    "gemini-generation",
                    as_type="generation",
                    model=get_settings().gemini_model,
                    input={
                        "prompt_version": "buyout-inspection-v4",
                        "chunk_ids": [hit.chunk_id for hit in hits],
                    },
                ) as generation_span:
                    call = (
                        self.generator.generate_profile(
                            identity, hits, "buyout-inspection-v4", progress=provider_progress
                        )
                        if isinstance(self.generator, GeminiProvider)
                        and type(self.generator).generate_profile is GeminiProvider.generate_profile
                        else self.generator.generate_profile(identity, hits, "buyout-inspection-v4")
                    )
                    # Whole-provider watchdog also covers retries/cleanup and injected adapters.
                    try:
                        generation = await asyncio.wait_for(
                            call, timeout=3 * get_settings().gemini_timeout_seconds + 4
                        )
                    except TimeoutError:
                        raise ProviderGenerationError("timeout", retryable=True) from None
                    generation_span.update(
                        output={
                            "validation_status": "pending_grounding_validation",
                            "latency_ms": generation.latency_ms,
                        },
                        usage_details={
                            "input_tokens": generation.input_tokens,
                            "output_tokens": generation.output_tokens,
                        },
                    )
            except ProviderGenerationError as exc:
                diagnostics.error_type = type(exc).__name__
                diagnostics.last_error = self._safe_error(exc)
                diagnostics.structured_validation_error = exc.detail
                diagnostics.structured_validation_errors = exc.validation_errors
                if exc.category == "invalid_structured_output":
                    diagnostics.structured_validation_status = "rejected"
                diagnostics.llm_latency_ms = max(
                    1, int((perf_counter() - generation_started) * 1000)
                )
                statuses = {
                    "rate_limited": "provider_rate_limited",
                    "provider_unavailable": "provider_temporarily_unavailable",
                    "network_error": "provider_temporarily_unavailable",
                    "timeout": "provider_timeout",
                    "authentication_failed": "provider_auth_error",
                    "invalid_request": "provider_invalid_request",
                    "model_unavailable": "provider_model_unavailable",
                    "invalid_structured_output": "invalid_response",
                }
                return BuildResult(
                    statuses.get(exc.category, "failed"),
                    diagnostics=diagnostics,
                    scope_key=identity.scope_key,
                )
            notify("validating")
            logger.info(
                "knowledge build=%s SDK response validated; local_evidence=%s", build_id, len(hits)
            )
            counts = ValidationCounts()
            try:
                profile = validate_grounded_profile(
                    generation.draft, hits, identity, reject_invalid=True, counts=counts
                )
            except (ValueError, TypeError) as exc:
                diagnostics.error_type = type(exc).__name__
                diagnostics.last_error = "structured_validation_failed"
                return BuildResult(
                    "structured_validation_failed",
                    diagnostics=diagnostics,
                    scope_key=identity.scope_key,
                )
            diagnostics.claims_generated = counts.claims_generated
            diagnostics.claims_rejected_no_evidence = counts.claims_rejected_no_evidence
            diagnostics.claims_rejected_scope_mismatch = counts.claims_rejected_scope_mismatch
            if profile.status == "insufficient_evidence" and counts.claims_generated:
                diagnostics.error_type = "GroundingValidationError"
                diagnostics.last_error = "generated_claims_lacked_valid_evidence"
                diagnostics.structured_validation_status = "rejected"
                return BuildResult(
                    "structured_validation_failed",
                    diagnostics=diagnostics,
                    scope_key=identity.scope_key,
                )
            claims = (
                profile.common_problems
                + profile.problematic_components
                + profile.inspection_points
                + profile.expensive_failures
            )
            diagnostics.high_evidence_items = sum(claim.confidence == "high" for claim in claims)
            diagnostics.medium_evidence_items = sum(
                claim.confidence == "medium" for claim in claims
            )
            diagnostics.limited_evidence_items = sum(
                claim.confidence == "limited" for claim in claims
            )
            diagnostics.structured_validation_status = "validated"
            diagnostics.input_tokens = generation.input_tokens
            diagnostics.output_tokens = generation.output_tokens
            diagnostics.llm_latency_ms = generation.latency_ms
            diagnostics.llm_model = generation.model
            diagnostics.llm_provider = (
                "Gemini" if isinstance(self.generator, GeminiProvider) else "custom"
            )
            diagnostics.prompt_version = generation.prompt_version
            if (
                profile.status == "insufficient_evidence"
                and existing
                and existing[0].status == "complete"
            ):
                return BuildResult(
                    "rebuild_preserved", existing[0], diagnostics, identity.scope_key
                )
            notify("saving")
            saved, _ = profiles.save(
                identity,
                profile,
                pipeline_version=self.pipeline_version,
                embedding_model=self.embedder.model_name,
                llm_model=generation.model,
                overwrite=force or bool(existing),
            )
            status = (
                "partial"
                if diagnostics.sources_failed and saved.status == "complete"
                else saved.status
            )
            logger.info("knowledge build=%s profile persisted status=%s", build_id, status)
            return BuildResult(status, saved, diagnostics, identity.scope_key)
        finally:
            connection.close()
