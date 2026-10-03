"""Bounded provider awaits and journal/status lifecycle, using synthetic data only."""

import asyncio
import json
import logging
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from google.genai import errors

from backend.api.knowledge import KnowledgeJobs
from backend.config import Settings
from backend.database.init_db import initialize_connection
from backend.knowledge.gemini import GeminiProvider, ProviderGenerationError
from backend.knowledge.identity import KnowledgeIdentity
from backend.knowledge.processing import chunk_document, process_html
from backend.knowledge.progress import runtime_timing
from backend.knowledge.repository import KnowledgeRepository
from backend.knowledge.retrieval import KnowledgeHit
from backend.knowledge.service import TechnicalKnowledgeService
from backend.services.marketplace_catalog import CatalogCache


class SDKClient:
    def __init__(self, *, stuck=False, failures=0):
        self.aio = SimpleNamespace(models=self, aclose=self.aclose)
        self.stuck = stuck
        self.failures = failures
        self.calls = []
        self.cancelled = False

    async def aclose(self):
        pass

    def close(self):
        pass

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.stuck:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if len(self.calls) <= self.failures:
            raise errors.ServerError(503, {"error": {"message": "fixture secret"}})
        evidence = json.loads(kwargs["contents"])["evidence"]
        return SimpleNamespace(
            text=json.dumps(
                {
                    "inspection_points": [
                        {
                            "title": "Осмотр доступных шлангов",
                            "description": "Проверить видимые следы течи на шлангах",
                            "inspection_category": "cooling",
                            "what_to_check": ["Осмотреть соединения шлангов на следы течи"],
                            "inspection_methods": ["visual"],
                            "evidence_refs": [evidence[0]["chunk_id"]],
                        }
                    ]
                }
            ),
            model_version="fixture-model",
            usage_metadata=None,
        )


def provider(client, timeout=0.15):
    return GeminiProvider(
        Settings(_env_file=None, GEMINI_API_KEY="fixture-only", GEMINI_TIMEOUT_SECONDS=timeout),
        client,
    )


def hit(identity):
    return KnowledgeHit(
        1,
        "Inspect hoses for visible leaks.",
        "Cooling",
        "cooling",
        identity.scope_key,
        ("https://example.test/manual",),
        -0.1,
        "hash",
        document_id=1,
        source_ids=(1,),
        source_types=("repair",),
        corpus_kind="production",
    )


def test_sdk_await_times_out_and_cancels_instead_of_waiting_forever():
    async def run():
        client = SDKClient(stuck=True)
        events = []
        identity = KnowledgeIdentity("synthetic-brand", "synthetic-model")
        with pytest.raises(ProviderGenerationError) as caught:
            await asyncio.wait_for(
                provider(client, 0.02).generate_profile(
                    identity,
                    [hit(identity)],
                    progress=lambda phase, fields: events.append((phase, fields)),
                ),
                timeout=1,
            )
        assert caught.value.category == "timeout"
        assert client.cancelled and len(client.calls) == 1
        assert events[0][0] == "calling_gemini"
        assert events[0][1]["provider_timeout_seconds"] == 0.02
        config = client.calls[0]["config"]
        assert config.automatic_function_calling.disable is True
        assert config.tools is None
        assert config.response_json_schema["properties"]["inspection_points"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid_fields, expected",
    [
        (
            {"evidence_refs": None},
            {
                "path": "inspection_points.0.evidence_refs",
                "type": "list_type",
                "message": "Input should be a valid array",
            },
        ),
        (
            {"requires_service": True},
            {
                "path": "inspection_points.0",
                "type": "value_error",
                "message": "Value error, Service-only item needs a service note",
            },
        ),
    ],
)
def test_profile_validation_errors_reach_diagnostics_without_raw_input(
    tmp_path, monkeypatch, caplog, invalid_fields, expected
):
    from backend.knowledge import gemini

    # Capture the adapter WARNING even when the app's knowledge logger has its own handler.
    monkeypatch.setattr(gemini.logger, "handlers", [caplog.handler])
    caplog.set_level(logging.WARNING, logger=gemini.logger.name)
    raw_marker = "RAW_EVIDENCE_MUST_NOT_BE_LOGGED"

    class InvalidSDKClient(SDKClient):
        async def generate_content(self, **kwargs):
            response = await super().generate_content(**kwargs)
            payload = json.loads(response.text)
            payload["inspection_points"][0].update(invalid_fields)
            payload["inspection_points"][0]["why_it_matters"] = raw_marker
            response.text = json.dumps(payload)
            return response

    class Embedder:
        model_name = "fixture"
        model_version = "1"

        async def embed(self, texts, *, query=False):
            return [[1.0, 0.0] for _ in texts]

    class NoDiscovery:
        timeout_seconds = 1

        async def discover(self, identity, client):
            raise AssertionError("Local production corpus must be reused")

    identity = KnowledgeIdentity("synthetic-brand", "synthetic-model")
    path = tmp_path / "validation-errors.db"
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        clean = process_html("<main><p>Inspect hoses for visible leaks before purchase.</p></main>")
        KnowledgeRepository(connection).save_document(
            identity,
            "https://example.test/manual",
            "repair",
            clean,
            chunk_document(clean, chunk_words=20, overlap_words=0),
        )

    async def run():
        client = InvalidSDKClient()
        adapter = provider(client)
        with pytest.raises(ProviderGenerationError) as caught:
            await adapter.generate_profile(identity, [hit(identity)])
        assert caught.value.detail == "schema_validation"
        assert caught.value.validation_errors == [expected]
        service = TechnicalKnowledgeService(
            path, grabber=NoDiscovery(), embedder=Embedder(), generator=adapter
        )
        jobs = KnowledgeJobs()
        assert jobs.start(identity, service)
        await asyncio.wait_for(jobs._tasks[identity.scope_key], timeout=3)
        status = jobs.status(identity.scope_key)
        persisted = service.build_status(identity)
        for diagnostics in (status, status["diagnostics"], persisted, persisted["diagnostics"]):
            assert diagnostics["structured_validation_errors"] == [expected]
            assert diagnostics["structured_validation_error"] == "schema_validation"
            assert diagnostics["structured_validation_status"] == "rejected"
            assert diagnostics["sdk_response_received"] is True
            assert raw_marker not in json.dumps(diagnostics)
        assert status["status"] == "invalid_response"
        assert status["usable_evidence_chunks"] == 1
        assert not persisted["profile_persisted"]
        warning = "\n".join(
            record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
        )
        assert expected["path"] in warning and expected["message"] in warning
        assert raw_marker not in warning and "fixture-only" not in warning
        assert all(
            set(error) == {"path", "type", "message"} for error in caught.value.validation_errors
        )
        # Read the actual SDK request string, not PowerShell's rendering of UTF-8 bytes.
        instruction = client.calls[-1]["config"].system_instruction
        assert instruction.startswith("Ты опытный специалист")
        assert "Пиши по-русски" in instruction and "\ufffd" not in instruction

    asyncio.run(run())


def test_bounded_retries_publish_backoff_and_attempt_then_structured_response(caplog):
    async def run():
        client = SDKClient(failures=2)
        identity = KnowledgeIdentity("synthetic-brand", "synthetic-model")
        events = []
        result = await provider(client).generate_profile(
            identity, [hit(identity)], progress=lambda phase, fields: events.append((phase, fields))
        )
        assert result.draft.inspection_points
        assert len(client.calls) == 3
        active = [fields for phase, fields in events if phase == "calling_gemini"]
        assert [entry["provider_attempt"] for entry in active] == [1, 2, 3]
        backoff = [fields for phase, fields in events if phase == "provider_retry"]
        assert [entry["provider_attempt"] for entry in backoff] == [2, 3]
        assert all(entry["retry_in_seconds"] > 0 for entry in backoff)
        assert events[-1][0] == "validating_gemini_response"
        assert events[-1][1]["sdk_response_received"]
        assert "fixture secret" not in caplog.text

    asyncio.run(run())


def test_sdk_hidden_retries_disabled(monkeypatch):
    from backend.knowledge import gemini

    client = SDKClient()
    options = []

    def create(**kwargs):
        options.append(kwargs["http_options"])
        return client

    monkeypatch.setattr(gemini.genai, "Client", create)
    identity = KnowledgeIdentity("synthetic-brand", "synthetic-model")
    asyncio.run(provider(None).generate_profile(identity, [hit(identity)]))
    assert options[0].retry_options.attempts == 1


def test_timing_is_derived_without_heartbeat_writes():
    result = runtime_timing(
        {
            "started_at": "2026-01-01T00:00:00+00:00",
            "phase_started_at": "2026-01-01T00:00:02+00:00",
            "finished_at": "2026-01-01T00:00:05+00:00",
            "provider_attempt_started_at": "2026-01-01T00:00:03+00:00",
        }
    )
    assert result["build_elapsed_seconds"] == 5
    assert result["elapsed_seconds"] == 3
    assert result["provider_elapsed_seconds"] == 2


def test_timeout_journal_status_preserves_profile_and_corpus_and_retry_reuses_it(tmp_path):
    class Embedder:
        model_name = "fixture"
        model_version = "1"

        async def embed(self, texts, *, query=False):
            return [[1.0, 0.0] for _ in texts]

    class NoDiscovery:
        timeout_seconds = 1

        async def discover(self, identity, client):
            raise AssertionError("Must reuse local production corpus")

    path = tmp_path / "runtime.db"
    identity = KnowledgeIdentity("synthetic-brand", "synthetic-model")
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        clean = process_html("<main><p>Inspect hoses for visible leaks before purchase.</p></main>")
        KnowledgeRepository(connection).save_document(
            identity,
            "https://example.test/manual",
            "repair",
            clean,
            chunk_document(clean, chunk_words=20, overlap_words=0),
        )

    async def run():
        service = TechnicalKnowledgeService(
            path, grabber=NoDiscovery(), embedder=Embedder(), generator=provider(SDKClient())
        )
        good = await service.build(identity)
        assert good.status == "complete"
        # Simulate corpus/profile written by the old POST-empty / GET-absent path.
        legacy_key = json.dumps(
            [identity.canonical_brand_id, identity.canonical_model_id, "", None, None],
            separators=(",", ":"),
        )
        with sqlite3.connect(path) as connection:
            for table in ("knowledge_sources", "knowledge_documents", "problem_profiles"):
                connection.execute(
                    f"UPDATE {table} SET scope_key=? WHERE scope_key=?",
                    (legacy_key, identity.scope_key),
                )
        old = service.cached(identity).profile
        service.generator = provider(SDKClient(stuck=True), timeout=0.2)
        jobs = KnowledgeJobs()
        assert jobs.start(identity, service, force=True)
        assert not jobs.start(identity, service, force=True)
        for _ in range(200):
            await asyncio.sleep(0.005)
            current = jobs.status(identity.scope_key)
            if current["phase"] == "calling_gemini" and current.get("provider_attempt"):
                break
        assert current["phase"] == "calling_gemini"
        assert current["provider_attempt"] == 1
        assert current["provider_max_attempts"] == 3
        assert current["provider_timeout_seconds"] == 0.2
        assert current["phase_started_at"] and current["updated_at"]
        assert current["build_elapsed_seconds"] >= 0
        await asyncio.wait_for(jobs._tasks[identity.scope_key], timeout=2)
        assert jobs.status(identity.scope_key)["status"] == "provider_timeout"
        persisted = service.build_status(identity)
        assert persisted["status"] == "provider_timeout"
        assert persisted["profile_persisted"] is False
        assert service.cached(identity).profile == old
        phases = []
        service.generator = provider(SDKClient())
        retried = await service.build(
            identity, force=True, progress=lambda phase, diag: phases.append(phase)
        )
        assert retried.status == "complete"
        assert retried.diagnostics.knowledge_path == "local_corpus"
        assert retried.diagnostics.documents_created == 0
        assert retried.diagnostics.chunks_created == 0
        assert retried.diagnostics.embeddings_created == 0
        assert [phase for phase in phases if phase in {"validating", "saving", "complete"}] == [
            "validating",
            "saving",
            "complete",
        ]

    asyncio.run(run())


def test_empty_post_ids_and_omitted_get_ids_share_the_same_active_job(tmp_path, monkeypatch):
    from backend.api import knowledge as api
    from backend.knowledge.service import BuildDiagnostics, BuildResult

    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "brands": [
                    {
                        "id": "synthetic-brand",
                        "name": "Example Brand",
                        "models": [
                            {"id": "synthetic-model", "name": "Example Model", "generations": []}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "get_catalog_cache", lambda: CatalogCache(catalog_path))
    monkeypatch.setattr(api, "jobs", KnowledgeJobs())

    class Service:
        generator = SimpleNamespace(configured=True)
        release = None

        def cached(self, identity):
            return None

        def build_status(self, identity):
            return None

        async def build(self, identity, *, force, refresh_sources, progress):
            progress(
                "calling_gemini",
                BuildDiagnostics(
                    provider="gemini",
                    provider_attempt=1,
                    provider_max_attempts=3,
                    provider_timeout_seconds=120,
                ),
            )
            await self.release.wait()
            return BuildResult("insufficient_evidence", scope_key=identity.scope_key)

    service = Service()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_technical_service] = lambda: service

    async def run():
        service.release = asyncio.Event()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            selection = {"brand": "Example Brand", "model": "Example Model"}
            post = (
                await client.post(
                    "/api/knowledge/build", json={**selection, "generation_id": "", "engine_id": ""}
                )
            ).json()
            await asyncio.sleep(0)
            status = (await client.get("/api/knowledge/status", params=selection)).json()
            assert status["status"] == "building"
            assert status["scope_key"] == post["scope_key"]
            assert json.loads(post["scope_key"])[2:] == [None, None, None]
            assert status["phase"] == "calling_gemini"
            assert status["provider_attempt"] == 1
            assert status["provider_timeout_seconds"] == 120
            service.release.set()
            await api.jobs._tasks[post["scope_key"]]
            assert (await client.get("/api/knowledge/status", params=selection)).json()[
                "status"
            ] == "insufficient_evidence"

    asyncio.run(run())


@pytest.mark.parametrize("usable", [True, False], ids=["cached-evidence", "rejected-evidence"])
def test_status_distinguishes_discovery_retrieval_and_usable_evidence(tmp_path, usable):
    """Zero ingestion counters must not imply empty context; rejected retrieval must skip SDK."""
    class Embedder:
        model_name = "fixture"
        model_version = "1"

        async def embed(self, texts, *, query=False):
            return [[1.0, 0.0] for _ in texts]

    class EmptyDiscovery:
        timeout_seconds = 1

        async def discover(self, identity, client):
            return []

    class WaitingClient(SDKClient):
        async def generate_content(self, **kwargs):
            self.entered.set()
            await self.release.wait()
            return await super().generate_content(**kwargs)

    identity = KnowledgeIdentity("synthetic-brand", "synthetic-model")
    path = tmp_path / "evidence-counters.db"
    content = (
        "Inspect hoses for visible leaks before purchase."
        if usable
        else "A colorful timeline of the town and local architecture."
    )
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        repository = KnowledgeRepository(connection)
        clean = process_html(f"<main><p>{content}</p></main>")
        chunks = chunk_document(clean, chunk_words=20, overlap_words=0)
        for url in ("https://example.test/manual", "https://another.test/manual"):
            # Two real source links, one deduplicated document/chunk.
            repository.save_document(identity, url, "repair", clean, chunks)

    async def run():
        client = WaitingClient(failures=1)
        client.entered = asyncio.Event()
        client.release = asyncio.Event()
        service = TechnicalKnowledgeService(
            path, grabber=EmptyDiscovery(), embedder=Embedder(), generator=provider(client, 5)
        )
        jobs = KnowledgeJobs()
        assert jobs.start(identity, service)
        if usable:
            await asyncio.wait_for(client.entered.wait(), timeout=3)
            active = jobs.status(identity.scope_key)
            persisted = service.build_status(identity)
            for status in (active, persisted, persisted["diagnostics"]):
                assert status["sources_discovered"] == 0
                assert status["sources_used"] == 2
                assert status["chunks_created"] == 0
                assert status["retrieved_chunks"] == 1
                assert status["usable_evidence_chunks"] == 1
                assert status["final_context_chunks"] == status["usable_evidence_chunks"]
            assert active["phase"] == persisted["phase"] == "calling_gemini"
            assert active["knowledge_path"] == "local_corpus"
            client.release.set()
        await asyncio.wait_for(jobs._tasks[identity.scope_key], timeout=3)
        final = jobs.status(identity.scope_key)
        persisted = service.build_status(identity)
        for status in (final, final["diagnostics"], persisted, persisted["diagnostics"]):
            assert status["sources_discovered"] == 0
            assert status["chunks_created"] == 0
            assert status["retrieved_chunks"] == 1
            assert status["sources_used"] == (2 if usable else 0)
            assert status["usable_evidence_chunks"] == (1 if usable else 0)
        if usable:
            assert final["status"] == "complete"
            assert len(client.calls) == 2  # Retries preserve context and evidence counters.
            assert all(
                len(json.loads(call["contents"])["evidence"]) == 1 for call in client.calls
            )
        else:
            assert final["status"] == "insufficient_evidence"
            assert final["hard_rejected_candidates"] == 1
            assert not client.entered.is_set() and client.calls == []
            assert final["structured_validation_status"] == "not_attempted"
            assert final["profile"]["source_count"] == final["profile"]["evidence_count"] == 0

    asyncio.run(run())
