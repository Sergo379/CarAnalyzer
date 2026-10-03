import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

from backend.api.knowledge import KnowledgeJobs, get_technical_service
from backend.api.knowledge import router as knowledge_router
from backend.config import Settings
from backend.database.init_db import initialize_connection
from backend.knowledge.evidence_policy import grade_evidence, select_usable_evidence
from backend.knowledge.gemini import (
    GeminiProvider,
    GenerationResult,
    ProviderGenerationError,
    ProviderNotConfiguredError,
)
from backend.knowledge.grabber import (
    DiscoveryAttempt,
    DiscoveryResult,
    LoadedSource,
    ProviderDiscoveryResult,
    SourceCandidate,
    TechnicalGrabber,
    WebSearchDiscovery,
    WikipediaDiscovery,
    canonical_url,
    technical_search_queries,
    validate_source_scope,
)
from backend.knowledge.identity import (
    KnowledgeIdentity,
    normalize_display_text,
    resolve_knowledge_identity,
)
from backend.knowledge.indexer import KnowledgeIndexer
from backend.knowledge.llama_ingest import LlamaIndexIngestAdapter
from backend.knowledge.observability import LangfuseObserver
from backend.knowledge.processing import chunk_document, process_html
from backend.knowledge.profile import (
    AnswerDraft,
    ClaimDraft,
    ProfileDraft,
    TechnicalProfile,
    ValidationCounts,
    validate_grounded_answer,
    validate_grounded_profile,
)
from backend.knowledge.profile_repository import ProfileRepository
from backend.knowledge.repository import KnowledgeRepository
from backend.knowledge.retrieval import HybridRetriever, KnowledgeHit
from backend.knowledge.service import BuildDiagnostics, BuildResult, TechnicalKnowledgeService
from backend.knowledge.vector_store import SqliteVectorStore
from backend.services.marketplace_catalog import CatalogCache
from lab1.evaluation.metrics import deterministic_generation_metrics, retrieval_metrics
from lab1.evaluation.runner import ExperimentConfig, evaluate_sync, load_benchmark, log_mlflow


def _graded_hit(
    identity: KnowledgeIdentity,
    *,
    chunk_id: int = 1,
    source_type: str = "repair",
    url: str = "https://technical.example.org/repair",
    score: float = -0.5,
    content: str = "Inspect turbo hoses for leaks before purchase.",
) -> KnowledgeHit:
    return KnowledgeHit(
        chunk_id,
        content,
        "Engine",
        "engine",
        identity.scope_key,
        (url,),
        score,
        f"hash-{chunk_id}",
        document_id=chunk_id,
        source_ids=(chunk_id,),
        source_types=(source_type,),
        corpus_kind="production",
    )


def _catalog(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "brands": [
                    {
                        "id": "brand-a",
                        "name": "Brand A",
                        "models": [
                            {
                                "id": "model-a",
                                "name": "Model A",
                                "generations": [
                                    {
                                        "id": "gen-1",
                                        "name": "G1",
                                        "year_from": 2018,
                                        "year_to": 2022,
                                        "modifications": [
                                            {
                                                "id": "mod-1",
                                                "name": "1.5",
                                                "engine": {"engine_code": "ENG1"},
                                            },
                                            {
                                                "id": "mod-2",
                                                "name": "2.0",
                                                "engine": {"engine_code": "ENG2"},
                                            },
                                        ],
                                    },
                                    {"id": "gen-2", "name": "G2", "modifications": []},
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return CatalogCache(path=path)


def test_canonical_knowledge_scope_and_safe_fallback(tmp_path) -> None:
    cache = _catalog(tmp_path)
    identity = resolve_knowledge_identity(
        cache, "Brand A", "Model A", "gen-1", modification_id="mod-1"
    )
    assert identity.canonical_model_id == "model-a"
    assert (identity.generation_year_from, identity.generation_year_to) == (2018, 2022)
    assert [item.canonical_modification_id for item in identity.fallback()] == [
        "mod-1",
        None,
        None,
        None,
    ]
    assert [item.canonical_engine_id for item in identity.fallback()][-2:] == [None, None]
    assert [item.canonical_generation_id for item in identity.fallback()][-2:] == ["gen-1", None]
    other = resolve_knowledge_identity(
        cache, "Brand A", "Model A", "gen-1", modification_id="mod-2"
    )
    assert other.scope_key not in {item.scope_key for item in identity.fallback()}
    with pytest.raises(ValueError, match="Generation"):
        resolve_knowledge_identity(cache, "Brand A", "Model A", "foreign")
    with pytest.raises(ValueError, match="Engine"):
        resolve_knowledge_identity(cache, "Brand A", "Model A", "gen-2", engine_id="mod-1")


def test_additive_schema_is_idempotent_and_preserves_old_data() -> None:
    with sqlite3.connect(":memory:") as connection:
        initialize_connection(connection)
        connection.execute(
            "INSERT INTO searches (brand, model, year, body_type, price, created_at) "
            "VALUES ('A', 'B', 2020, 'sedan', 100, '2026-01-01')"
        )
        initialize_connection(connection)
        assert connection.execute("SELECT count(*) FROM searches").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='knowledge_chunks_fts'"
            ).fetchone()[0]
            == 1
        )


def test_cleaning_chunk_metadata_and_duplicate_document_skip() -> None:
    html = """<html class='site-header-enabled'><head><title>Technical</title></head><body>
    <nav>Menu</nav><main><h1>Engine</h1><p>Check code P0299 and turbo pressure.</p>
    <ul><li>Inspect hoses</li></ul><div class='cookie-banner'>Accept cookies</div>
    </main><footer>Contact us</footer></body></html>"""
    clean = process_html(html)
    assert "P0299" in clean.content
    assert "Menu" not in clean.content
    assert "Accept cookies" not in clean.content
    chunks = chunk_document(clean, "section", chunk_words=20, overlap_words=5)
    assert chunks[0].section == "Engine"
    assert chunks[0].component == "engine"
    identity = KnowledgeIdentity("brand-a", "model-a")
    with sqlite3.connect(":memory:") as connection:
        initialize_connection(connection)
        repo = KnowledgeRepository(connection)
        first = repo.save_document(identity, "https://example.org/one", "article", clean, chunks)
        second = repo.save_document(identity, "https://example.org/two", "article", clean, chunks)
        third = repo.save_document(identity, "https://example.org/one", "article", clean, chunks)
        assert first.document_created
        assert not second.document_created and second.chunks_created == 0
        assert not third.document_created
        assert first.document_id == second.document_id == third.document_id
        assert repo.source_count(identity.scope_key) == 2
        assert connection.execute("SELECT count(*) FROM knowledge_chunks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM knowledge_chunks_fts").fetchone()[0] == 1
        metadata = json.loads(
            connection.execute("SELECT metadata FROM knowledge_chunks").fetchone()[0]
        )
        assert metadata["canonical_model_id"] == "model-a"
        assert metadata["component"] == "engine"


def test_bounded_grabber_deduplicates_and_isolates_failures() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a")

    class BrokenProvider:
        async def discover(self, identity, client, limit):
            raise httpx.ConnectError("blocked")

    class FixtureProvider:
        async def discover(self, identity, client, limit):
            return [
                SourceCandidate(
                    "https://docs.example.org/car?utm_source=test", "A", "article", identity
                ),
                SourceCandidate("https://docs.example.org/car", "A duplicate", "article", identity),
                SourceCandidate("http://127.0.0.1/private", "unsafe", "article", identity),
                SourceCandidate("https://docs.example.org/other", "B", "article", identity),
            ]

    grabber = TechnicalGrabber([BrokenProvider(), FixtureProvider()], max_sources=3, per_domain=1)

    async def run():
        async with httpx.AsyncClient() as client:
            return await grabber.discover(identity, client)

    import asyncio

    candidates = asyncio.run(run())
    assert len(candidates) == 1
    assert candidates[0].url == "https://docs.example.org/car"
    assert canonical_url("https://example.org/x#part") == "https://example.org/x"
    with pytest.raises(ValueError):
        canonical_url("http://localhost/x")


def test_dynamic_multilingual_queries_and_provider_fallback_are_generic() -> None:
    import asyncio

    identity = KnowledgeIdentity(
        "brand-x",
        "model-y",
        "generation-z",
        "engine-q",
        brand="Example Brand",
        model="Example Model",
        generation="Generation Z",
        engine="Engine Q",
    )
    queries = technical_search_queries(identity, 10)
    assert len(queries) == 10
    assert all("Example Brand" in query and "Example Model" in query for query, _, _ in queries)
    assert {language for _, _, language in queries} == {"en", "ru"}
    assert any("common problems" in query for query, _, _ in queries)
    assert any("типичные проблемы" in query for query, _, _ in queries)
    assert {intent for _, intent, _ in queries} == {
        "common_problems",
        "body",
        "suspension_steering",
        "engine_cooling",
        "drivetrain",
        "brakes_electronics",
        "maintenance",
        "recall",
        "inspection_forum",
    }

    class EmptyProvider:
        name = "empty"

        async def discover(self, identity, client, limit):
            return ProviderDiscoveryResult(
                [], [DiscoveryAttempt(self.name, "empty query", "zero_results")]
            )

    class UsefulProvider:
        name = "useful"

        async def discover(self, identity, client, limit):
            candidates = [
                SourceCandidate(
                    "https://docs.example.org/item?utm_source=one",
                    "Technical item",
                    "technical_article",
                    identity,
                    self.name,
                    "dynamic query",
                ),
                SourceCandidate(
                    "https://docs.example.org/item",
                    "Technical item duplicate",
                    "technical_article",
                    identity,
                    self.name,
                    "dynamic query",
                ),
            ]
            return ProviderDiscoveryResult(
                candidates, [DiscoveryAttempt(self.name, "dynamic query", "success", 2)]
            )

    async def run():
        async with httpx.AsyncClient() as client:
            return await TechnicalGrabber(
                [EmptyProvider(), UsefulProvider()], max_sources=4
            ).discover_with_report(identity, client)

    result = asyncio.run(run())
    assert [attempt.provider for attempt in result.attempts] == ["empty", "useful"]
    assert len(result.candidates) == 1
    assert result.candidates[0].url == "https://docs.example.org/item"


def test_failed_web_provider_falls_back_and_mediawiki_remains_supplemental() -> None:
    import asyncio

    identity = KnowledgeIdentity("brand-x", "model-y", brand="Example Brand", model="Example Model")

    class UnavailableProvider:
        name = "web_primary"

        async def discover(self, identity, client, limit):
            raise OSError("primary resolver offline")

    class SecondaryProvider:
        name = "web_secondary"

        async def discover(self, identity, client, limit):
            candidate = SourceCandidate(
                "https://technical.example.org/guide",
                "Example Brand Example Model reliability guide",
                "technical_article",
                identity,
                self.name,
                "reliability query",
            )
            return ProviderDiscoveryResult(
                [candidate],
                [DiscoveryAttempt(self.name, "reliability query", "success", 1)],
            )

    class SupplementalProvider:
        name = "mediawiki"

        async def discover(self, identity, client, limit):
            candidate = SourceCandidate(
                "https://en.wikipedia.org/wiki/Example_Model",
                "Example Model",
                "encyclopedia",
                identity.fallback()[-1],
                self.name,
                "Example Brand Example Model",
            )
            return ProviderDiscoveryResult(
                [candidate],
                [DiscoveryAttempt(self.name, "Example Brand Example Model", "success", 1)],
            )

    page = (
        "<main><p>Example Brand Example Model technical reliability, engine, transmission, "
        "suspension and inspection information. " + ("Known issue detail. " * 40) + "</p></main>"
    )

    def respond(request):
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            grabber = TechnicalGrabber(
                [UnavailableProvider(), SecondaryProvider(), SupplementalProvider()],
                max_sources=4,
            )
            discovery = await grabber.discover_with_report(identity, client)
            loaded = [await grabber.load(item, client) for item in discovery.candidates]
            return discovery, loaded

    discovery, loaded = asyncio.run(run())
    assert [attempt.provider for attempt in discovery.attempts] == [
        "web_primary",
        "web_secondary",
        "mediawiki",
    ]
    assert discovery.attempts[0].status == "network_error"
    assert discovery.attempts[0].error_type == "OSError"
    assert discovery.attempts[0].error_message == "primary resolver offline"
    assert {item.source_type for item in discovery.candidates} == {
        "technical_article",
        "encyclopedia",
    }
    assert all(item.status == "complete" and item.document for item in loaded)

    with sqlite3.connect(":memory:") as connection:
        initialize_connection(connection)
        repository = KnowledgeRepository(connection)
        for item in loaded:
            assert item.document is not None
            repository.save_document(
                item.candidate.identity,
                item.candidate.url,
                item.candidate.source_type,
                item.document,
                chunk_document(item.document, chunk_words=30, overlap_words=0),
            )
        assert repository.source_count(identity.scope_key) == 2


def test_unicode_generation_metadata_stays_clean_through_query_builder() -> None:
    identity = KnowledgeIdentity(
        "brand-x",
        "model-y",
        "gen-z",
        brand="Марка",
        model="Модель",
        generation=normalize_display_text("E3 Â· 3 Ð¿Ð¾ÐºÐ¾Ð»ÐµÐ½Ð¸Ðµ"),
    )
    query = technical_search_queries(identity, 1)[0][0]
    assert identity.generation == "E3 · 3 поколение"
    assert '"Марка" "Модель" "E3 · 3 поколение"' in query
    assert "Â" not in query and "Ð" not in query


def test_source_scope_relevance_rejects_mismatch_and_keeps_generic_as_model_fallback() -> None:
    requested = KnowledgeIdentity(
        "brand-x",
        "model-y",
        "gen-z",
        brand="Brand X",
        model="Model Y",
        generation="G3",
        generation_year_from=2017,
        generation_year_to=2023,
    )
    document = process_html(
        "<main><p>Brand X Model Y suspension and engine technical information. "
        "Inspect all components before purchase.</p></main>"
    )
    incompatible = validate_source_scope(
        requested,
        LoadedSource(
            SourceCandidate(
                "https://example.org/old",
                "2013 Brand X Model Y problems",
                "technical_article",
                requested,
            ),
            document,
            "complete",
        ),
    )
    assert incompatible.status == "incompatible_scope" and incompatible.document is None

    generic = validate_source_scope(
        requested,
        LoadedSource(
            SourceCandidate(
                "https://example.org/generic",
                "Brand X Model Y maintenance guide",
                "technical_article",
                requested,
            ),
            document,
            "complete",
        ),
    )
    assert generic.document is not None
    assert generic.candidate.identity.canonical_generation_id is None

    exact = validate_source_scope(
        requested,
        LoadedSource(
            SourceCandidate(
                "https://example.org/current",
                "2019 Brand X Model Y G3 problems",
                "technical_article",
                requested,
            ),
            document,
            "complete",
        ),
    )
    assert exact.document is not None
    assert exact.candidate.identity.canonical_generation_id == "gen-z"


def test_web_search_reports_provider_unavailable(monkeypatch) -> None:
    import asyncio

    def unavailable(query, language, limit, backend, timeout_seconds):
        raise OSError("offline")

    monkeypatch.setattr(WebSearchDiscovery, "_search", staticmethod(unavailable))
    identity = KnowledgeIdentity("brand-x", "model-y", brand="Example Brand", model="Example Model")

    async def run():
        async with httpx.AsyncClient() as client:
            return await WebSearchDiscovery(max_queries=2).discover(identity, client, 4)

    outcome = asyncio.run(run())
    assert outcome.candidates == []
    assert outcome.attempts[-1].status == "network_error"
    assert outcome.attempts[-1].error_type == "OSError"
    assert outcome.attempts[-1].error_message == "offline"


def test_duplicate_provider_query_attempts_are_collapsed_but_languages_are_distinct() -> None:
    import asyncio

    identity = KnowledgeIdentity("brand-x", "model-y")

    class DuplicateProvider:
        name = "fixture"

        async def discover(self, identity, client, limit):
            return ProviderDiscoveryResult(
                [],
                [
                    DiscoveryAttempt(self.name, "same query", "success", 1, language="en"),
                    DiscoveryAttempt(self.name, "same query", "success", 1, language="en"),
                    DiscoveryAttempt(self.name, "same query", "success", 1, language="ru"),
                ],
            )

    async def run():
        async with httpx.AsyncClient() as client:
            return await TechnicalGrabber([DuplicateProvider()]).discover_with_report(
                identity, client
            )

    result = asyncio.run(run())
    assert [(item.provider, item.query, item.language) for item in result.attempts] == [
        ("fixture", "same query", "en"),
        ("fixture", "same query", "ru"),
    ]


def test_grabber_load_statuses_without_captcha_bypass() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a")
    candidate = SourceCandidate("https://example.org/doc", "Doc", "article", identity)
    page = "<main><h1>Engine</h1><p>" + ("Technical detail P0299. " * 40) + "</p></main>"

    def respond(request):
        if request.url.path == "/blocked":
            return httpx.Response(403)
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            grabber = TechnicalGrabber()
            good = await grabber.load(candidate, client)
            blocked = await grabber.load(
                SourceCandidate("https://example.org/blocked", "Blocked", "article", identity),
                client,
            )
            return good, blocked

    import asyncio

    good, blocked = asyncio.run(run())
    assert good.status == "complete" and good.document is not None
    assert blocked.status == "blocked" and blocked.document is None


def test_pdf_loader_is_bounded_and_bad_pdf_does_not_abort() -> None:
    import asyncio
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    buffer = BytesIO()
    writer.write(buffer)
    identity = KnowledgeIdentity("brand-a", "model-a")

    def respond(request):
        data = buffer.getvalue() if request.url.path == "/empty" else b"not a PDF"
        return httpx.Response(200, content=data, headers={"content-type": "application/pdf"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            grabber = TechnicalGrabber()
            empty = await grabber.load(
                SourceCandidate("https://example.org/empty", "Empty", "manual", identity), client
            )
            bad = await grabber.load(
                SourceCandidate("https://example.org/bad", "Bad", "manual", identity), client
            )
            return empty, bad

    empty, bad = asyncio.run(run())
    assert empty.status == "empty_document"
    assert bad.status == "parse_error"


def test_wikipedia_discovery_skips_failed_language_and_unrelated_titles() -> None:
    import asyncio

    identity = KnowledgeIdentity("brand-a", "model-a", brand="Brand A", model="Model A")

    def respond(request):
        if request.url.host == "en.wikipedia.org":
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "query": {
                    "search": [
                        {"title": "Brand A Model A (first generation)"},
                        {"title": "Brand A Other Model"},
                        {"title": "Brand A Model A"},
                    ]
                }
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await WikipediaDiscovery().discover(identity, client, 3)

    outcome = asyncio.run(run())
    assert len(outcome.candidates) == 1
    assert outcome.candidates[0].identity.scope_key == identity.scope_key
    assert outcome.candidates[0].url.startswith("https://ru.wikipedia.org/wiki/Brand_A_Model_A")
    assert {attempt.provider for attempt in outcome.attempts} == {"mediawiki"}


def test_llamaindex_ingestion_preserves_scope_and_sections() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a", "gen-a")
    document = process_html(
        "<main><h1>Engine</h1><p>" + ("Turbo code P0299 requires inspection. " * 40) + "</p></main>"
    )
    chunks = LlamaIndexIngestAdapter(chunk_tokens=128, overlap_tokens=16).chunk(document, identity)
    assert chunks
    assert all(chunk.section == "Engine" and chunk.component == "engine" for chunk in chunks)


def test_sqlite_vec_fts_hybrid_scope_filter_and_embedding_cache() -> None:
    import asyncio

    class FakeEmbedding:
        model_name = "fixture-multilingual"
        model_version = "1"

        async def embed(self, texts, *, query=False):
            return [[1.0, 0.0] if "P0299" in text else [0.0, 1.0] for text in texts]

    with sqlite3.connect(":memory:") as connection:
        initialize_connection(connection)
        repo = KnowledgeRepository(connection)
        requested = KnowledgeIdentity("brand-a", "model-a", "gen-a", "engine-a")
        wrong_engine = KnowledgeIdentity("brand-a", "model-a", "gen-a", "engine-b")
        wrong_generation = KnowledgeIdentity("brand-a", "model-a", "gen-b")
        generic = KnowledgeIdentity("brand-a", "model-a")
        docs = [
            (requested, "https://one.example.org/a", "Engine P0299 turbo pressure issue"),
            (wrong_engine, "https://two.example.org/b", "Engine P0299 wrong engine"),
            (wrong_generation, "https://three.example.org/c", "Engine P0299 wrong generation"),
            (generic, "https://four.example.org/d", "General inspection checklist"),
        ]
        ids = []
        for scope, url, text in docs:
            document = process_html(f"<main><h1>Engine</h1><p>{text}</p></main>")
            chunks = chunk_document(document, chunk_words=20, overlap_words=0)
            saved = repo.save_document(scope, url, "article", document, chunks)
            ids.append(saved.document_id)
        vectors = SqliteVectorStore(connection)
        indexer = KnowledgeIndexer(connection, FakeEmbedding(), vectors, batch_size=2)
        assert sum(asyncio.run(indexer.index_document(item)) for item in ids) == 4
        assert sum(asyncio.run(indexer.index_document(item)) for item in ids) == 0
        retriever = HybridRetriever(connection, vectors)
        hits, diagnostics = retriever.retrieve(
            "P0299 turbo",
            requested,
            query_vector=[1.0, 0.0],
            embedding_model="fixture-multilingual",
            embedding_version="1",
            top_k=10,
        )
        assert diagnostics.lexical_candidates >= 1
        assert diagnostics.vector_candidates >= 1
        assert hits and "issue" in hits[0].content
        assert all(
            hit.scope_key in {scope.scope_key for scope in requested.fallback()} for hit in hits
        )
        assert not any("wrong" in hit.content for hit in hits)
        assert vectors.search("fixture-multilingual", "1", [1.0, 0.0])


def test_reranker_changes_hybrid_order_without_creating_evidence() -> None:
    class ReverseReranker:
        def rerank(self, query: str, hits: list[KnowledgeHit], limit: int):
            return list(reversed(hits))[:limit]

    identity = KnowledgeIdentity("brand-a", "model-a")
    with sqlite3.connect(":memory:") as connection:
        initialize_connection(connection)
        repo = KnowledgeRepository(connection)
        for suffix, content in (("a", "engine turbo"), ("b", "turbo engine pressure")):
            doc = process_html(f"<main><p>{content}</p></main>")
            repo.save_document(
                identity,
                f"https://{suffix}.example.org/doc",
                "article",
                doc,
                chunk_document(doc, chunk_words=20, overlap_words=0),
            )
        plain, _ = HybridRetriever(connection).retrieve("engine turbo", identity, top_k=2)
        reranked, _ = HybridRetriever(connection, reranker=ReverseReranker()).retrieve(
            "engine turbo", identity, top_k=2
        )
        assert [hit.chunk_id for hit in reranked] == [hit.chunk_id for hit in reversed(plain)]


class FakeGeminiClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = []
        self.aio = SimpleNamespace(models=self)

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            text=json.dumps(self.payload),
            model_version="fixture-gemini",
            usage_metadata=SimpleNamespace(prompt_token_count=20, candidates_token_count=30),
        )


def test_gemini_keyless_status_and_strict_structured_response() -> None:
    import asyncio

    identity = KnowledgeIdentity("brand-a", "model-a")
    hit = KnowledgeHit(
        1,
        "P0299 indicates turbo pressure inspection.",
        "Engine",
        "engine",
        identity.scope_key,
        ("https://one.example.org/doc",),
        0.5,
        "hash",
    )
    missing = GeminiProvider(Settings(_env_file=None, GEMINI_API_KEY=None))
    assert not missing.configured
    with pytest.raises(ProviderNotConfiguredError):
        asyncio.run(missing.generate_profile(identity, [hit]))
    client = FakeGeminiClient(
        {
            "common_problems": [
                {
                    "title": "Давление турбины",
                    "description": "Проверить сигнал P0299",
                    "component": "engine",
                    "evidence_refs": [1],
                    "buyer_checks": ["Проверить индикаторы на приборной панели"],
                    "inspection_methods": ["controls"],
                }
            ],
            "risk_summary": "Проверить контур наддува.",
            "risk_summary_evidence_refs": [1],
        }
    )
    provider = GeminiProvider(Settings(_env_file=None, GEMINI_API_KEY="fixture-only"), client)
    result = asyncio.run(provider.generate_profile(identity, [hit]))
    assert result.input_tokens == 20 and result.output_tokens == 30
    assert result.draft.common_problems[0].evidence_refs == [1]
    assert "P0299" in client.calls[0]["contents"]
    assert client.calls[0]["config"].response_mime_type == "application/json"
    assert client.calls[0]["config"].response_json_schema is not None


def test_gemini_rejects_invalid_structured_schema() -> None:
    with pytest.raises(ValueError):
        ProfileDraft.model_validate_json('{"invented_field": "unsafe"}')
    with pytest.raises(ValueError):
        ClaimDraft(
            title="Unsafe",
            description="Unsupported procedure",
            evidence_refs=[1],
            requires_service=True,
        )


def test_gemini_requires_russian_narrative_but_allows_english_system_name() -> None:
    import asyncio

    identity = KnowledgeIdentity("synthetic-brand", "synthetic-model")
    base = {
        "evidence_refs": [1],
        "inspection_category": "engine",
        "system_name": "Engine Cooling System",
    }
    russian = FakeGeminiClient(
        {
            "inspection_points": [
                {
                    **base,
                    "title": "Проверка охлаждения",
                    "description": "Осмотрите доступные шланги",
                    "what_to_check": ["Осмотреть шланги"],
                }
            ]
        }
    )
    provider = GeminiProvider(Settings(_env_file=None, GEMINI_API_KEY="fixture-only"), russian)
    result = asyncio.run(provider.generate_profile(identity, [_graded_hit(identity)]))
    assert result.draft.inspection_points[0].system_name == "Engine Cooling System"

    english = FakeGeminiClient(
        {
            "inspection_points": [
                {
                    **base,
                    "title": "Inspect cooling",
                    "description": "Inspect hoses for leaks",
                }
            ]
        }
    )
    provider = GeminiProvider(Settings(_env_file=None, GEMINI_API_KEY="fixture-only"), english)
    with pytest.raises(ProviderGenerationError, match="invalid_structured_output"):
        asyncio.run(provider.generate_profile(identity, [_graded_hit(identity)]))


def test_gemini_503_retries_then_succeeds_without_secret_in_errors() -> None:
    import asyncio

    class IntermittentClient(FakeGeminiClient):
        async def generate_content(self, **kwargs):
            if len(self.calls) < 2:
                self.calls.append(kwargs)
                raise genai_errors.ServerError(503, {"error": {"message": "busy"}})
            return await super().generate_content(**kwargs)

    client = IntermittentClient({"inspection_points": []})
    provider = GeminiProvider(Settings(_env_file=None, GEMINI_API_KEY="fixture-only"), client)
    identity = KnowledgeIdentity("brand-a", "model-a")
    result = asyncio.run(provider.generate_profile(identity, [_graded_hit(identity)]))
    assert result.model == "fixture-gemini"
    assert len(client.calls) == 3


@pytest.mark.parametrize(
    ("code", "category", "calls"),
    [(503, "provider_unavailable", 3), (429, "rate_limited", 1), (401, "authentication_failed", 1)],
)
def test_gemini_errors_are_distinct_and_bounded(code, category, calls) -> None:
    import asyncio

    class RefusingClient(FakeGeminiClient):
        async def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            error = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
            raise error(code, {"error": {"message": "secret fixture-only must not leak"}})

    client = RefusingClient({})
    provider = GeminiProvider(Settings(_env_file=None, GEMINI_API_KEY="fixture-only"), client)
    identity = KnowledgeIdentity("brand-a", "model-a")
    with pytest.raises(ProviderGenerationError) as caught:
        asyncio.run(provider.generate_profile(identity, [_graded_hit(identity)]))
    assert caught.value.category == category
    assert "fixture-only" not in str(caught.value)
    assert len(client.calls) == calls


def test_graded_gemini_prompt_receives_provenance_scope_and_tier() -> None:
    import asyncio

    identity = KnowledgeIdentity("brand-a", "model-a")
    hit = replace(_graded_hit(identity, source_type="owner_forum"), evidence_tier="limited")

    client = FakeGeminiClient({"inspection_points": []})
    provider = GeminiProvider(Settings(_env_file=None, GEMINI_API_KEY="fixture-only"), client)
    assert (
        asyncio.run(provider.generate_profile(identity, [hit])).prompt_version
        == "buyout-inspection-v4"
    )
    evidence = json.loads(client.calls[0]["contents"])["evidence"][0]
    assert evidence["chunk_id"] == hit.chunk_id
    assert evidence["source_type"] == ["owner_forum"]
    assert evidence["domain"] == ["technical.example.org"]
    assert evidence["scope_key"] == identity.scope_key
    assert evidence["evidence_tier"] == "limited"


def test_no_evidence_no_claim_and_permanent_profile_cache() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a", "gen-a")
    hit = KnowledgeHit(
        1,
        "Engine code P0299 requires turbo inspection.",
        "Engine",
        "engine",
        identity.scope_key,
        ("https://one.example.org/doc",),
        0.8,
        "hash",
        document_id=1,
        source_ids=(1,),
        source_types=("repair",),
        corpus_kind="production",
    )
    draft = ProfileDraft(
        common_problems=[
            ClaimDraft(
                title="P0299 issue",
                description="Inspect turbo for P0299",
                component="engine",
                evidence_refs=[1],
            )
        ]
    )
    profile = validate_grounded_profile(draft, [hit], identity)
    assert profile.status == "complete" and profile.confidence == "medium"
    assert validate_grounded_profile(ProfileDraft(), [], identity).status == "insufficient_evidence"
    with pytest.raises(ValueError, match="missing"):
        validate_grounded_profile(
            ProfileDraft(
                common_problems=[
                    ClaimDraft(
                        title="Turbo fault",
                        description="Turbo fault confirmed",
                        evidence_refs=[999],
                    )
                ]
            ),
            [hit],
            identity,
        )
    with pytest.raises(ValueError, match="technical code"):
        validate_grounded_profile(
            ProfileDraft(
                common_problems=[
                    ClaimDraft(
                        title="P9999 fault", description="Inspect P9999 code", evidence_refs=[1]
                    )
                ]
            ),
            [hit],
            identity,
        )
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_connection(connection)
        doc = process_html("<main><p>Engine code P0299 requires turbo inspection.</p></main>")
        saved = KnowledgeRepository(connection).save_document(
            identity,
            "https://one.example.org/doc",
            "article",
            doc,
            chunk_document(doc, chunk_words=20, overlap_words=0),
        )
        actual_chunk = connection.execute(
            "SELECT id FROM knowledge_chunks WHERE document_id=?", (saved.document_id,)
        ).fetchone()[0]
        assert actual_chunk == 1
        repo = ProfileRepository(connection)
        assert repo.save(
            identity, profile, pipeline_version="1", embedding_model="fixture", llm_model="fixture"
        )[1]
        assert not repo.save(
            identity, profile, pipeline_version="1", embedding_model="fixture", llm_model="fixture"
        )[1]
        assert repo.get(identity)[0] == profile
        assert connection.execute("SELECT count(*) FROM claim_evidence").fetchone()[0] == 1
        assert repo.sources(identity)[0]["chunk_ids"] == [1]


def test_grounded_question_answer_rejects_invented_codes() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a")
    hit = KnowledgeHit(
        1,
        "Code P0299 indicates low boost.",
        "Engine",
        "engine",
        identity.scope_key,
        ("https://example.org/doc",),
        0.8,
        "hash",
    )
    assert validate_grounded_answer(
        AnswerDraft(answer="P0299 indicates low boost", evidence_refs=[1]), [hit], identity
    ).answer
    with pytest.raises(ValueError, match="technical code"):
        validate_grounded_answer(
            AnswerDraft(answer="P9999 indicates low boost", evidence_refs=[1]), [hit], identity
        )
    with pytest.raises(ValueError, match="missing"):
        validate_grounded_answer(
            AnswerDraft(answer="Low boost", evidence_refs=[2]), [hit], identity
        )


def test_buyout_claim_without_observable_check_is_service_only() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a")
    hit = _graded_hit(identity)
    claim = ClaimDraft(
        title="Состояние узла",
        description="Источник сообщает о проблеме с узлом",
        evidence_refs=[hit.chunk_id],
    )
    profile = validate_grounded_profile(ProfileDraft(common_problems=[claim]), [hit], identity)
    assert profile.common_problems[0].requires_service is True
    assert profile.common_problems[0].inspection_methods == ["service_required"]
    assert profile.common_problems[0].service_note
    with_check = claim.model_copy(
        update={"buyer_checks": ["Осмотреть шланги на утечки"], "inspection_methods": ["visual"]}
    )
    profile_with_check = validate_grounded_profile(
        ProfileDraft(common_problems=[with_check]), [hit], identity
    )
    assert profile_with_check.common_problems[0].requires_service is False
    unsupported_check = claim.model_copy(
        update={"buyer_checks": ["Прислушаться к шуму"], "inspection_methods": ["sound"]}
    )
    downgraded = validate_grounded_profile(
        ProfileDraft(common_problems=[unsupported_check]), [hit], identity
    )
    assert downgraded.common_problems[0].requires_service is True
    assert downgraded.common_problems[0].buyer_checks == []


def test_gemini_question_mode_uses_only_bounded_evidence_and_valid_json() -> None:
    import asyncio

    identity = KnowledgeIdentity("brand-a", "model-a", brand="Brand A", model="Model A")
    hit = KnowledgeHit(
        1,
        "Code P0299 means low boost.",
        "Engine",
        "engine",
        identity.scope_key,
        ("https://example.org/doc",),
        0.7,
        "hash",
    )

    client = FakeGeminiClient(
        {
            "answer": "P0299 means low boost",
            "evidence_refs": [1],
            "insufficient_evidence": False,
        }
    )
    provider = GeminiProvider(Settings(_env_file=None, GEMINI_API_KEY="fixture-only"), client)
    result = asyncio.run(
        provider.answer_question(
            identity, "What does P0299 mean?", [hit], "grounded-conservative-v2"
        )
    )
    assert result.draft.evidence_refs == [1]
    assert result.input_tokens == 20 and result.output_tokens == 30
    assert "Code P0299" in client.calls[0]["contents"]


def test_service_reuses_persisted_profile_without_second_llm_call(tmp_path) -> None:
    import asyncio

    class FakeEmbedding:
        model_name = "fixture"
        model_version = "1"
        calls = 0

        async def embed(self, texts, *, query=False):
            self.calls += 1
            return [[1.0, 0.0] for _ in texts]

    class FakeGenerator:
        configured = True
        calls = 0

        async def generate_profile(self, identity, hits, prompt_version="grounded-v1"):
            self.calls += 1
            return GenerationResult(
                ProfileDraft(
                    inspection_points=[
                        ClaimDraft(
                            title="Turbo inspection",
                            description="Inspect turbo pressure",
                            evidence_refs=[hits[0].chunk_id],
                        )
                    ]
                ),
                "fixture-model",
                10,
                10,
                1,
                prompt_version,
            )

    path = tmp_path / "knowledge.db"
    identity = KnowledgeIdentity(
        "brand-a", "model-a", "gen-a", brand="Example Brand", model="Example Model"
    )
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        clean = process_html(
            "<main><h1>Engine</h1><p>Example Brand Example Model: "
            "inspect turbo pressure and hoses.</p></main>"
        )
        KnowledgeRepository(connection).save_document(
            identity,
            "https://example.org/manual",
            "repair",
            clean,
            chunk_document(clean, chunk_words=20, overlap_words=0),
        )
    generator = FakeGenerator()

    class MustNotDiscover:
        timeout_seconds = 1.0
        calls = 0

        async def discover(self, identity, client):
            self.calls += 1
            raise AssertionError("Local production corpus must be checked first")

    grabber = MustNotDiscover()
    embedder = FakeEmbedding()
    service = TechnicalKnowledgeService(
        path, grabber=grabber, embedder=embedder, generator=generator
    )
    first = asyncio.run(service.build(identity))
    calls_after_first = embedder.calls
    second = asyncio.run(service.build(identity))
    assert first.status == "complete"
    assert second.status == "cached"
    assert generator.calls == 1

    assert grabber.calls == 0
    assert embedder.calls == calls_after_first
    assert second.profile == first.profile
    assert first.diagnostics.knowledge_path == "local_corpus"
    assert first.diagnostics.vector_candidates >= 1
    assert first.diagnostics.lexical_candidates >= 1
    assert first.diagnostics.embeddings_created >= 1
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM claim_evidence")
        connection.execute("DELETE FROM problem_claims")
        connection.execute("DELETE FROM problem_profiles WHERE scope_key=?", (identity.scope_key,))
        assert connection.execute("SELECT count(*) FROM knowledge_embeddings").fetchone()[0] >= 1
    rebuilt = asyncio.run(service.build(identity))
    assert rebuilt.status == "complete"
    assert rebuilt.diagnostics.knowledge_path == "local_corpus"
    assert rebuilt.diagnostics.embeddings_created == 0
    assert rebuilt.diagnostics.vector_candidates >= 1
    assert rebuilt.diagnostics.lexical_candidates >= 1
    assert grabber.calls == 0
    assert generator.calls == 2
    third = asyncio.run(service.build(identity, force=True))
    assert third.status == "complete"
    assert generator.calls == 3
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM problem_profiles").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM claim_evidence").fetchone()[0] == 1


def test_no_source_result_can_retry_discovery(tmp_path) -> None:
    import asyncio

    class EmptyGrabber:
        timeout_seconds = 1.0
        calls = 0

        async def discover(self, identity, client):
            self.calls += 1
            return []

    class ConfiguredGenerator:
        configured = True

    class FakeEmbedding:
        model_name = "fixture"
        model_version = "1"

    grabber = EmptyGrabber()
    service = TechnicalKnowledgeService(
        tmp_path / "empty.db",
        grabber=grabber,
        embedder=FakeEmbedding(),
        generator=ConfiguredGenerator(),
    )
    identity = KnowledgeIdentity("brand-a", "model-a")
    first = asyncio.run(service.build(identity))
    second = asyncio.run(service.build(identity))
    assert first.status == "no_sources"
    assert first.profile.status == "insufficient_evidence"
    assert second.status == "no_sources"
    assert grabber.calls == 2


def test_complete_model_fallback_beats_insufficient_exact_profile(tmp_path) -> None:
    path = tmp_path / "fallback.db"
    model = KnowledgeIdentity("brand-a", "model-a")
    generation = KnowledgeIdentity("brand-a", "model-a", "gen-a")
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        profiles = ProfileRepository(connection)
        for scope, status in ((model, "complete"), (generation, "insufficient_evidence")):
            profiles.save(
                scope,
                TechnicalProfile(status=status),
                pipeline_version="test",
                embedding_model="test",
                llm_model="test",
            )
        found = profiles.get(generation)
        assert found is not None
        assert found[0].status == "complete"
        assert found[1].scope_key == model.scope_key


def test_failed_forced_rebuild_preserves_complete_profile(tmp_path) -> None:
    import asyncio

    class EmptyGrabber:
        timeout_seconds = 1.0

        async def discover(self, identity, client):
            return []

    class ConfiguredGenerator:
        configured = True

    class FakeEmbedding:
        model_name = "fixture"
        model_version = "1"

    path = tmp_path / "preserved.db"
    identity = KnowledgeIdentity("brand-a", "model-a")
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        ProfileRepository(connection).save(
            identity,
            TechnicalProfile(status="complete"),
            pipeline_version="test",
            embedding_model="test",
            llm_model="test",
        )
    service = TechnicalKnowledgeService(
        path,
        grabber=EmptyGrabber(),
        embedder=FakeEmbedding(),
        generator=ConfiguredGenerator(),
    )
    result = asyncio.run(service.build(identity, force=True))
    assert result.status == "rebuild_preserved"
    assert service.cached(identity).profile.status == "complete"


def test_new_exact_scope_discovers_despite_unrelated_global_corpus_and_caches(tmp_path) -> None:
    import asyncio

    class FakeEmbedding:
        model_name = "fixture"
        model_version = "1"

        async def embed(self, texts, *, query=False):
            return [
                [1.0, 0.0] if query or "turbo pressure" in text.casefold() else [0.0, 1.0]
                for text in texts
            ]

    class FakeIngest:
        def chunk(self, document, identity):
            return chunk_document(document, chunk_words=20, overlap_words=0)

    class OneSourceGrabber:
        timeout_seconds = 1.0
        calls = 0

        async def discover_with_report(self, identity, client):
            self.calls += 1
            query = '"Example Brand" "Example Model" common problems'
            candidates = [
                SourceCandidate(
                    "https://manual.example/new",
                    "Manual",
                    "repair",
                    identity,
                    "fixture_search",
                    query,
                ),
                SourceCandidate(
                    "https://blocked.example/new",
                    "Blocked",
                    "owner_forum",
                    identity,
                    "fixture_search",
                    query,
                ),
            ]
            return DiscoveryResult(
                candidates,
                [DiscoveryAttempt("fixture_search", query, "complete", len(candidates))],
            )

        async def load(self, candidate, client):
            if "blocked" in candidate.url:
                return LoadedSource(candidate, None, "blocked")
            document = process_html(
                "<main><h1>Engine</h1><p>"
                "Example Brand Example Model G1: inspect turbo pressure and oil hoses "
                "before purchase."
                "</p></main>"
            )
            return LoadedSource(candidate, document, "complete")

    class GroundedGenerator:
        configured = True
        calls = 0
        received = []

        async def generate_profile(self, identity, hits, prompt_version="grounded-v1"):
            self.calls += 1
            self.received = hits
            return GenerationResult(
                ProfileDraft(
                    inspection_points=[
                        ClaimDraft(
                            title="Turbo inspection",
                            description="Inspect turbo pressure and oil hoses",
                            evidence_refs=[hits[0].chunk_id],
                        )
                    ]
                ),
                "fixture-model",
                8,
                5,
                1,
                prompt_version,
            )

    path = tmp_path / "isolated.db"
    requested = KnowledgeIdentity(
        "brand-a",
        "model-a",
        "gen-a",
        brand="Example Brand",
        model="Example Model",
        generation="G1",
    )
    other = KnowledgeIdentity("other-brand", "other-model")
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        for scope, url in ((other, "https://other.example/doc"),):
            old = process_html("<main><p>Legacy broad material without a fault.</p></main>")
            KnowledgeRepository(connection).save_document(
                scope,
                url,
                "manual",
                old,
                chunk_document(old, chunk_words=20, overlap_words=0),
            )
    grabber = OneSourceGrabber()
    generator = GroundedGenerator()
    service = TechnicalKnowledgeService(
        path,
        grabber=grabber,
        embedder=FakeEmbedding(),
        generator=generator,
        ingest_adapter=FakeIngest(),
    )
    first = asyncio.run(service.build(requested))
    second = asyncio.run(service.build(requested))
    assert first.status == "partial" and second.status == "cached"
    assert grabber.calls == 1 and generator.calls == 1
    assert first.diagnostics.queries_generated == 1
    assert first.diagnostics.sources_failed == 1
    assert generator.received
    assert any(hit.scope_key == requested.scope_key for hit in generator.received)
    assert all("other-brand" not in hit.scope_key for hit in generator.received)
    assert first.diagnostics.context_evidence_count == len(generator.received)
    assert first.diagnostics.context_source_count >= 1
    with sqlite3.connect(path) as connection:
        linked = connection.execute(
            """SELECT c.id, c.document_id, s.id, s.url
               FROM claim_evidence ce
               JOIN knowledge_chunks c ON c.id=ce.chunk_id
               JOIN knowledge_source_documents sd ON sd.document_id=c.document_id
               JOIN knowledge_sources s ON s.id=sd.source_id"""
        ).fetchall()
        assert linked and linked[0][3] == "https://manual.example/new"
        attempts = KnowledgeRepository(connection).discovery_attempts(requested)
        assert attempts[0]["provider"] == "fixture_search"
        source = next(
            item
            for item in ProfileRepository(connection).sources(requested)
            if item["url"] == "https://manual.example/new"
        )
        assert source["discovery_provider"] == "fixture_search"
        assert source["generated_query"]
        assert source["chunk_count"] >= 1


def test_production_retrieval_excludes_evaluation_unlinked_and_unrelated_chunks() -> None:
    requested = KnowledgeIdentity("brand-a", "model-a", "gen-a")
    unrelated = KnowledgeIdentity("brand-a", "model-b", "gen-a")
    with sqlite3.connect(":memory:") as connection:
        initialize_connection(connection)
        repo = KnowledgeRepository(connection)
        for identity, url, corpus in (
            (requested, "https://evaluation.example/doc", "evaluation"),
            (unrelated, "https://unrelated.example/doc", "production"),
        ):
            document = process_html("<main><p>Turbo pressure evidence phrase.</p></main>")
            repo.save_document(
                identity,
                url,
                "synthetic" if corpus == "evaluation" else "manual",
                document,
                chunk_document(document, chunk_words=20, overlap_words=0),
                corpus_kind=corpus,
            )
        cursor = connection.execute(
            """INSERT INTO knowledge_documents
               (scope_key, content_hash, content, language, fetched_at)
               VALUES (?, 'unlinked-doc', 'Turbo pressure evidence phrase.', 'en', '2026-01-01')""",
            (requested.scope_key,),
        )
        connection.execute(
            """INSERT INTO knowledge_chunks
               (document_id, chunk_index, content, content_hash, language)
               VALUES (?, 0, 'Turbo pressure evidence phrase.', 'unlinked-chunk', 'en')""",
            (cursor.lastrowid,),
        )
        hits, diagnostics = HybridRetriever(connection).retrieve(
            "turbo pressure evidence", requested, top_k=10
        )
        assert hits == []
        assert diagnostics.final_context_chunks == 0


def test_zero_production_evidence_skips_gemini(tmp_path) -> None:
    import asyncio

    class EmptyGrabber:
        timeout_seconds = 1.0
        calls = 0

        async def discover(self, identity, client):
            self.calls += 1
            return []

    class TrackingGenerator:
        configured = True
        calls = 0

        async def generate_profile(self, identity, hits, prompt_version="grounded-v1"):
            self.calls += 1
            raise AssertionError("Generator must not be called without production evidence")

    class FakeEmbedding:
        model_name = "fixture"
        model_version = "1"

    path = tmp_path / "no-production.db"
    identity = KnowledgeIdentity("brand-a", "model-a")
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        document = process_html("<main><p>Synthetic turbo evidence only.</p></main>")
        KnowledgeRepository(connection).save_document(
            identity,
            "https://evaluation.example/doc",
            "synthetic",
            document,
            chunk_document(document, chunk_words=20, overlap_words=0),
            corpus_kind="evaluation",
        )
    grabber = EmptyGrabber()
    generator = TrackingGenerator()
    result = asyncio.run(
        TechnicalKnowledgeService(
            path, grabber=grabber, embedder=FakeEmbedding(), generator=generator
        ).build(identity)
    )
    assert result.status == "no_sources"
    assert result.diagnostics.final_context_chunks == 0
    assert grabber.calls == 1 and generator.calls == 0


@pytest.mark.parametrize(
    ("category", "status"),
    [
        ("timeout", "provider_timeout"),
        ("provider_unavailable", "provider_temporarily_unavailable"),
        ("rate_limited", "provider_rate_limited"),
        ("authentication_failed", "provider_auth_error"),
        ("invalid_request", "provider_invalid_request"),
        ("model_unavailable", "provider_model_unavailable"),
        ("invalid_structured_output", "invalid_response"),
    ],
)
def test_gemini_failure_statuses_are_not_cached_and_retry_reuses_corpus(
    tmp_path, category, status
) -> None:
    import asyncio

    class FakeEmbedding:
        model_name = "fixture"
        model_version = "1"

        async def embed(self, texts, *, query=False):
            return [[1.0, 0.0] for _ in texts]

    class FailingGemini(GeminiProvider):
        @property
        def configured(self):
            return True

        async def generate_profile(self, identity, hits, prompt_version="grounded-v1"):
            raise ProviderGenerationError(category, retryable=category != "authentication_failed")

    path = tmp_path / "gemini-timeout.db"
    identity = KnowledgeIdentity("brand-a", "model-a")
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        document = process_html("<main><p>Engine inspection evidence and known issue.</p></main>")
        KnowledgeRepository(connection).save_document(
            identity,
            "https://manual.example/doc",
            "repair",
            document,
            chunk_document(document, chunk_words=20, overlap_words=0),
        )
    service = TechnicalKnowledgeService(path, embedder=FakeEmbedding(), generator=FailingGemini())
    result = asyncio.run(service.build(identity))
    assert result.status == status
    assert result.diagnostics.final_context_chunks >= 1
    assert result.diagnostics.error_type == "ProviderGenerationError"
    assert result.diagnostics.last_error == category
    assert service.cached(identity) is None
    restarted = TechnicalKnowledgeService(path, embedder=FakeEmbedding(), generator=FailingGemini())
    persisted = restarted.build_status(identity)
    assert persisted is not None
    assert persisted["status"] == status
    assert persisted["phase"] == status
    assert persisted["error_type"] == "ProviderGenerationError"
    assert persisted["last_error"] == category
    assert persisted["profile_persisted"] is False

    class SuccessfulGenerator:
        configured = True

        async def generate_profile(self, identity, hits, prompt_version="grounded-v1"):
            return GenerationResult(
                ProfileDraft(
                    inspection_points=[
                        ClaimDraft(
                            title="Engine inspection",
                            description="Inspect the documented engine issue",
                            evidence_refs=[hits[0].chunk_id],
                        )
                    ]
                ),
                "fixture",
                4,
                4,
                1,
                prompt_version,
            )

    class MustNotDiscover:
        timeout_seconds = 1.0

        async def discover(self, identity, client):
            raise AssertionError("Provider retry must use the persisted corpus")

    retry = TechnicalKnowledgeService(
        path,
        grabber=MustNotDiscover(),
        embedder=FakeEmbedding(),
        generator=SuccessfulGenerator(),
    )
    recovered = asyncio.run(retry.build(identity))
    assert recovered.status == "complete"
    assert recovered.diagnostics.knowledge_path == "local_corpus"
    assert recovered.diagnostics.embeddings_created == 0
    assert retry.cached(identity).profile.status == "complete"


def test_downstream_exception_is_persisted_and_retry_reuses_existing_corpus(tmp_path) -> None:
    import asyncio

    class FakeEmbedding:
        model_name = "fixture"
        model_version = "1"

        async def embed(self, texts, *, query=False):
            return [[1.0, 0.0] for _ in texts]

    class ExplodingGenerator:
        configured = True

        async def generate_profile(self, identity, hits, prompt_version="grounded-v1"):
            raise RuntimeError("downstream validation exploded")

    class SuccessfulGenerator:
        configured = True

        async def generate_profile(self, identity, hits, prompt_version="grounded-v1"):
            return GenerationResult(
                ProfileDraft(
                    inspection_points=[
                        ClaimDraft(
                            title="Inspect engine",
                            description="Inspect the documented engine issue",
                            evidence_refs=[hits[0].chunk_id],
                        )
                    ]
                ),
                "fixture-model",
                10,
                5,
                1,
                prompt_version,
            )

    class MustNotDiscover:
        timeout_seconds = 1.0
        calls = 0

        async def discover(self, identity, client):
            self.calls += 1
            raise AssertionError("Persisted exact-scope corpus must be reused")

    path = tmp_path / "persistent-failure.db"
    identity = KnowledgeIdentity("brand-a", "model-a")
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        document = process_html(
            "<main><p>Brand A Model A documented engine issue and inspection evidence.</p></main>"
        )
        KnowledgeRepository(connection).save_document(
            identity,
            "https://manual.example/doc",
            "repair",
            document,
            chunk_document(document, chunk_words=20, overlap_words=0),
        )

    failing = TechnicalKnowledgeService(
        path,
        grabber=MustNotDiscover(),
        embedder=FakeEmbedding(),
        generator=ExplodingGenerator(),
    )
    with pytest.raises(RuntimeError, match="downstream validation exploded"):
        asyncio.run(failing.build(identity))
    failed = TechnicalKnowledgeService(
        path, embedder=FakeEmbedding(), generator=SuccessfulGenerator()
    ).build_status(identity)
    assert failed is not None and failed["status"] == "failed"
    assert failed["error_type"] == "RuntimeError"

    grabber = MustNotDiscover()
    retry = TechnicalKnowledgeService(
        path,
        grabber=grabber,
        embedder=FakeEmbedding(),
        generator=SuccessfulGenerator(),
    )
    result = asyncio.run(retry.build(identity))
    assert result.status == "complete"
    assert result.diagnostics.documents_created == 0
    assert result.diagnostics.chunks_created == 0
    assert result.diagnostics.embeddings_created == 0
    assert grabber.calls == 0
    persisted = retry.build_status(identity)
    assert persisted is not None and persisted["profile_persisted"] is True


def test_knowledge_api_is_separate_and_reports_keyless_status(tmp_path, monkeypatch) -> None:
    cache = _catalog(tmp_path)
    monkeypatch.setattr("backend.api.knowledge.get_catalog_cache", lambda: cache)

    class FakeService:
        generator = type("Generator", (), {"configured": False})()

        def cached(self, identity):
            return None

        def sources(self, identity):
            return []

        def discovery_attempts(self, identity):
            return []

    app = FastAPI()
    app.include_router(knowledge_router)
    app.dependency_overrides[get_technical_service] = FakeService
    with TestClient(app) as client:
        assert client.get("/api/knowledge/config").json() == {"provider_configured": False}
        params = {"brand": "Brand A", "model": "Model A", "generation_id": "gen-1"}
        profile = client.get("/api/knowledge/profile", params=params)
        assert profile.status_code == 200
        assert profile.json()["status"] == "provider_not_configured"
        build = client.post("/api/knowledge/build", json=params)
        assert build.status_code == 200 and build.json()["status"] == "provider_not_configured"
        assert client.get("/api/knowledge/sources", params=params).json()["sources"] == []
        invalid = client.get("/api/knowledge/profile", params={**params, "generation_id": "wrong"})
        assert invalid.status_code == 422


def test_knowledge_jobs_expose_live_phase_and_reject_duplicate_start() -> None:
    import asyncio

    identity = KnowledgeIdentity("brand-a", "model-a", "gen-a")

    class SlowService:
        def __init__(self):
            self.release = asyncio.Event()
            self.calls = 0

        async def build(self, identity, *, force, refresh_sources, progress):
            self.calls += 1
            progress(
                "discovering_sources",
                BuildDiagnostics(queries_generated=4, sources_discovered=2),
            )
            await self.release.wait()
            return BuildResult(
                "insufficient_evidence",
                TechnicalProfile(status="insufficient_evidence"),
                BuildDiagnostics(queries_generated=4, sources_discovered=2),
                identity.scope_key,
            )

    async def run():
        service = SlowService()
        jobs = KnowledgeJobs()
        assert jobs.start(identity, service)
        assert not jobs.start(identity, service)
        await asyncio.sleep(0)
        active = jobs.status(identity.scope_key)
        assert active is not None
        assert active["status"] == "building"
        assert active["phase"] == "discovering_sources"
        assert active["queries_generated"] == 4
        assert active["sources_discovered"] == 2
        assert active["started_at"]
        service.release.set()
        await jobs._tasks[identity.scope_key]
        complete = jobs.status(identity.scope_key)
        assert complete is not None
        assert complete["status"] == "insufficient_evidence"
        assert complete["phase"] == "insufficient_evidence"
        assert service.calls == 1

    asyncio.run(run())


def test_api_marks_broader_cached_profile_as_fallback(tmp_path, monkeypatch) -> None:
    cache = _catalog(tmp_path)
    monkeypatch.setattr("backend.api.knowledge.get_catalog_cache", lambda: cache)
    broad = KnowledgeIdentity("brand-a", "model-a")

    class FallbackService:
        generator = type("Generator", (), {"configured": True})()

        def cached(self, identity):
            return BuildResult(
                "cached",
                TechnicalProfile(status="complete"),
                scope_key=broad.scope_key,
            )

    app = FastAPI()
    app.include_router(knowledge_router)
    app.dependency_overrides[get_technical_service] = FallbackService
    with TestClient(app) as client:
        response = client.get(
            "/api/knowledge/profile",
            params={"brand": "Brand A", "model": "Model A", "generation_id": "gen-1"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "fallback_cached"
    assert response.json()["scope_level"] == "model"


def test_pad_dataset_metrics_and_local_mlflow(tmp_path) -> None:
    _, questions = load_benchmark()
    assert len(questions) == 40
    assert any(not question["answerable"] for question in questions)
    assert any(len(question["expected_evidence"]) > 1 for question in questions)
    metrics = retrieval_metrics(["other", "turbo"], ["turbo"], 2)
    assert metrics.recall_at_k == 1 and metrics.mrr == 0.5
    result = evaluate_sync(ExperimentConfig(chunk_strategy="fixed", chunk_words=40))
    assert result["question_count"] == 40
    assert result["summary"]["recall_at_k"] > 0
    run_id = log_mlflow(result, tmp_path / "tracking")
    assert run_id


def test_generation_proxy_metrics_and_langfuse_offline_noop() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a")
    hit = KnowledgeHit(
        1,
        "Inspect the turbocharger hoses.",
        "Engine",
        "engine",
        identity.scope_key,
        ("https://example.org/doc",),
        0.5,
        "hash",
    )
    draft = ProfileDraft(
        inspection_points=[
            ClaimDraft(
                title="Turbo inspection",
                description="Inspect turbocharger hoses",
                evidence_refs=[1],
            )
        ]
    )
    scores = deterministic_generation_metrics(draft, [hit], ["turbocharger hoses"])
    assert scores == {"faithfulness_citation_proxy": 1.0, "answer_relevance_fact_proxy": 1.0}
    observer = LangfuseObserver(
        Settings(_env_file=None, LANGFUSE_PUBLIC_KEY=None, LANGFUSE_SECRET_KEY=None)
    )
    assert not observer.enabled
    with observer.span("test", input={"scope_key": identity.scope_key}) as span:
        span.update(output={"status": "ok"})


def test_graded_policy_keeps_weak_real_evidence_but_rejects_unsafe_or_irrelevant() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a", "gen-a", "engine-a")
    weak = _graded_hit(identity)
    sibling = _graded_hit(KnowledgeIdentity("brand-a", "model-a", "gen-b"), chunk_id=2)
    no_source = replace(weak, chunk_id=3, content_hash="hash-3", source_ids=())
    synthetic = replace(weak, chunk_id=4, content_hash="hash-4", corpus_kind="evaluation")
    irrelevant = replace(
        weak,
        chunk_id=5,
        content_hash="hash-5",
        section="History",
        component="",
        content="A colorful timeline of the town and local architecture.",
    )
    extreme = replace(weak, chunk_id=6, content_hash="hash-6", score=-20)
    selection = select_usable_evidence(
        [weak, sibling, no_source, synthetic, irrelevant, extreme], identity, reranked=True
    )
    assert [hit.chunk_id for hit in selection.hits] == [1]
    assert selection.hard_rejected == 5
    assert selection.tiers == {"high": 0, "medium": 1, "limited": 0}


def test_grading_preserves_scope_owner_reports_and_independence() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a", "gen-a", "engine-a")
    exact = _graded_hit(identity, score=0.8)
    independent = _graded_hit(
        identity,
        chunk_id=2,
        url="https://manual.example.net/repair",
        score=0.7,
    )
    same_article = replace(
        exact,
        chunk_id=3,
        content_hash="different-hash",
        score=0.7,
    )
    owner = _graded_hit(identity, source_type="owner_forum")
    model = _graded_hit(KnowledgeIdentity("brand-a", "model-a"))
    assert grade_evidence([exact], identity) == "medium"
    assert grade_evidence([exact, independent], identity) == "high"
    assert grade_evidence([exact, same_article], identity) != "high"
    www_mirror = replace(
        independent,
        source_urls=("https://www.technical.example.org/mirror",),
    )
    assert grade_evidence([exact, www_mirror], identity) != "high"
    assert grade_evidence([owner], identity) == "limited"
    assert grade_evidence([model], identity) == "limited"
    assert grade_evidence([model], KnowledgeIdentity("brand-a", "model-a")) == "limited"
    draft = ProfileDraft(
        inspection_points=[
            ClaimDraft(
                title="Check turbo hoses",
                description="Inspect turbo hoses for leaks",
                evidence_refs=[1],
            )
        ]
    )
    owner_profile = validate_grounded_profile(draft, [owner], identity)
    assert owner_profile.inspection_points[0].confidence == "limited"
    assert "сообщениям владельцев" in owner_profile.inspection_points[0].description
    model_profile = validate_grounded_profile(draft, [model], identity)
    assert model_profile.inspection_points[0].scope_label == "model"
    assert "не о конкретном поколении/двигателе" in model_profile.inspection_points[0].description


def test_grading_rejects_claim_without_valid_provenance_and_migrates_cache() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a")
    hit = _graded_hit(identity)
    invalid = replace(hit, source_ids=())
    draft = ProfileDraft(
        inspection_points=[
            ClaimDraft(
                title="Check turbo hoses",
                description="Inspect turbo hoses for leaks",
                evidence_refs=[1],
            )
        ]
    )
    counts = ValidationCounts()
    result = validate_grounded_profile(
        draft, [invalid], identity, reject_invalid=True, counts=counts
    )
    assert result.status == "insufficient_evidence"
    assert counts.claims_rejected_no_evidence == 1
    wrong = _graded_hit(KnowledgeIdentity("brand-a", "other-model"))
    mismatch_counts = ValidationCounts()
    wrong_result = validate_grounded_profile(
        draft, [wrong], identity, reject_invalid=True, counts=mismatch_counts
    )
    assert wrong_result.status == "insufficient_evidence"
    assert mismatch_counts.claims_rejected_scope_mismatch == 1
    with pytest.raises(ValueError, match="evidence"):
        ClaimDraft(title="Check turbo", description="Inspect turbo hoses", evidence_refs=[])
    old = TechnicalProfile.model_validate(
        {
            "status": "complete",
            "confidence": "low",
            "inspection_points": [
                {
                    **draft.inspection_points[0].model_dump(),
                    "category": "inspection_points",
                    "scope_key": identity.scope_key,
                    "support_source_count": 1,
                    "confidence": "low",
                }
            ],
        }
    )
    assert old.confidence == "limited"
    assert old.inspection_points[0].confidence == "limited"
    single_model_cache = TechnicalProfile.model_validate(
        {
            "status": "complete",
            "confidence": "medium",
            "source_count": 1,
            "inspection_points": [
                {
                    **draft.inspection_points[0].model_dump(),
                    "category": "inspection_points",
                    "scope_key": identity.scope_key,
                    "scope_label": "model",
                    "support_source_count": 1,
                    "independent_source_count": 1,
                    "confidence": "medium",
                }
            ],
        }
    )
    assert single_model_cache.confidence == "limited"
    assert single_model_cache.inspection_points[0].confidence == "limited"


def test_expensive_failure_needs_explicit_repair_or_cost_evidence() -> None:
    identity = KnowledgeIdentity("brand-a", "model-a")
    hit = _graded_hit(identity, content="The transmission shudders during acceleration.")
    draft = ProfileDraft(
        expensive_failures=[
            ClaimDraft(
                title="Transmission replacement",
                description="The transmission needs expensive replacement.",
                evidence_refs=[hit.chunk_id],
            )
        ]
    )
    counts = ValidationCounts()
    result = validate_grounded_profile(draft, [hit], identity, reject_invalid=True, counts=counts)
    assert result.status == "insufficient_evidence"
    assert counts.claims_rejected_no_evidence == 1
    supported = replace(hit, content="Major transmission replacement is expensive.")
    assert validate_grounded_profile(draft, [supported], identity).status == "complete"


def test_service_calls_generator_for_negative_score_evidence_but_not_irrelevant(tmp_path) -> None:
    import asyncio

    class FakeEmbedding:
        model_name = "fixture"
        model_version = "1"

        async def embed(self, texts, *, query=False):
            return [[1.0, 0.0] for _ in texts]

    class NegativeReranker:
        def rerank(self, query, hits, limit):
            return [replace(hit, score=-0.5) for hit in hits[:limit]]

    class FakeGenerator:
        configured = True

        def __init__(self):
            self.calls = 0

        async def generate_profile(self, identity, hits, prompt_version="grounded-v1"):
            self.calls += 1
            assert hits[0].evidence_tier in {"medium", "limited"}
            assert prompt_version == "buyout-inspection-v4"
            return GenerationResult(
                ProfileDraft(
                    inspection_points=[
                        ClaimDraft(
                            title="Check turbo hoses",
                            description="Inspect turbo hoses for leaks",
                            evidence_refs=[hits[0].chunk_id],
                        )
                    ]
                ),
                "fixture",
                10,
                10,
                1,
                prompt_version,
            )

    path = tmp_path / "graded-service.db"
    relevant = KnowledgeIdentity("brand-a", "model-a", "gen-a")
    irrelevant = KnowledgeIdentity("brand-b", "model-b", "gen-b")
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        repo = KnowledgeRepository(connection)
        for identity, content, title in (
            (relevant, "Inspect turbo hoses for leaks before purchase.", "Engine"),
            (irrelevant, "A colorful timeline of the town and local architecture.", "History"),
        ):
            document = process_html(f"<main><h1>{title}</h1><p>{content}</p></main>")
            repo.save_document(
                identity,
                f"https://{identity.canonical_brand_id}.example.org/doc",
                "repair",
                document,
                chunk_document(document, chunk_words=20, overlap_words=0),
            )
    generator = FakeGenerator()

    class NoDiscovery:
        timeout_seconds = 1.0

        async def discover(self, identity, client):
            return []

    service = TechnicalKnowledgeService(
        path,
        grabber=NoDiscovery(),
        embedder=FakeEmbedding(),
        generator=generator,
        reranker=NegativeReranker(),
    )
    accepted = asyncio.run(service.build(relevant))
    assert accepted.status == "complete"
    assert accepted.diagnostics.usable_context_chunks > 0
    assert generator.calls == 1
    assert asyncio.run(service.build(relevant)).status == "cached"
    assert generator.calls == 1
    rejected = asyncio.run(service.build(irrelevant))
    assert rejected.status == "insufficient_evidence"
    assert rejected.diagnostics.hard_rejected_candidates > 0
    assert rejected.diagnostics.usable_context_chunks == 0
    assert generator.calls == 1


def test_buyout_checks_are_grounded_and_unsupported_service_interval_removed() -> None:
    identity = KnowledgeIdentity("synthetic-brand", "synthetic-model", "synthetic-generation")
    examples = [
        ("body", "Rust visible around door seals", "visual", "Осмотреть кромки дверей"),
        ("suspension", "Knock noise on road from suspension", "sound", "Слушать стук"),
        ("engine", "Cold start engine noise", "cold_start", "Слушать холодный запуск"),
        (
            "drivetrain",
            "Vibration while driving from drivetrain",
            "test_drive",
            "Проверить вибрацию на ходу",
        ),
        ("electronics", "Display warning indicator on dash", "controls", "Проверить дисплей"),
    ]
    hits = [
        _graded_hit(identity, chunk_id=index, content=content)
        for index, (_, content, _, _) in enumerate(examples, 1)
    ]
    hits.append(
        _graded_hit(
            identity,
            chunk_id=6,
            content="Service records confirm transmission fluid replacement history.",
        )
    )
    claims = [
        ClaimDraft(
            title=f"Проверка узла {index}",
            description="Подтверждённая особенность узла",
            system_name="Example System",
            inspection_category=category,
            issue_summary="Есть признаки износа",
            why_it_matters="Влияет на решение о выкупе",
            what_to_check=[check],
            how_to_check=[check],
            warning_signs=["Наблюдаемый симптом"],
            inspection_methods=[method],
            evidence_refs=[index],
        )
        for index, (category, _, method, check) in enumerate(examples, 1)
    ]
    claims.append(
        ClaimDraft(
            title="История обслуживания трансмиссии",
            description="Подтверждённая история обслуживания",
            inspection_category="service_history",
            evidence_refs=[6],
            what_to_check=["Сверить документы"],
            inspection_methods=["documents"],
            service_history_checks=["Проверить замену жидкости", "Проверить замену через 80000 км"],
        )
    )
    profile = validate_grounded_profile(ProfileDraft(inspection_points=claims), hits, identity)
    assert profile.status == "complete"
    assert profile.profile_version == 2
    assert {claim.inspection_category for claim in profile.inspection_points} == {
        "body",
        "suspension",
        "engine",
        "drivetrain",
        "electronics",
        "service_history",
    }
    assert all(claim.evidence_refs for claim in profile.inspection_points)
    assert all(claim.what_to_check for claim in profile.inspection_points)
    history = profile.inspection_points[-1]
    assert history.service_history_checks == ["Проверить замену жидкости"]
    assert not history.requires_service
    vague = validate_grounded_profile(
        ProfileDraft(
            inspection_points=[
                ClaimDraft(
                    title="Общая проверка двигателя",
                    description="Шум при холодном запуске",
                    inspection_category="engine",
                    what_to_check=["Проверить двигатель"],
                    inspection_methods=["cold_start"],
                    evidence_refs=[3],
                )
            ]
        ),
        hits,
        identity,
    ).inspection_points[0]
    assert vague.what_to_check == []
    assert vague.requires_service


def test_service_history_requires_evidence_for_every_interval() -> None:
    identity = KnowledgeIdentity("synthetic-brand", "synthetic-model", "synthetic-generation")
    hit = _graded_hit(
        identity,
        chunk_id=1,
        content="История обслуживания: замена жидкости через 40000 км подтверждена документами.",
    )
    profile = validate_grounded_profile(
        ProfileDraft(
            inspection_points=[
                ClaimDraft(
                    title="История замены жидкости",
                    description="Документы подтверждают обслуживание жидкости",
                    inspection_category="service_history",
                    evidence_refs=[1],
                    inspection_methods=["documents"],
                    service_history_checks=[
                        "Сверить замену через 40000 км",
                        "Сверить замену через 40000 км и через 80000 км",
                    ],
                )
            ]
        ),
        [hit],
        identity,
    )
    assert profile.inspection_points[0].service_history_checks == ["Сверить замену через 40000 км"]
