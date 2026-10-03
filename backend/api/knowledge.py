"""Separate asynchronous technical knowledge API; never on /api/search path."""

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.config import get_settings
from backend.knowledge.grabber import TechnicalGrabber, WebSearchDiscovery, WikipediaDiscovery
from backend.knowledge.identity import KnowledgeIdentity, resolve_knowledge_identity
from backend.knowledge.progress import runtime_timing
from backend.knowledge.retrieval import LocalCrossEncoderReranker
from backend.knowledge.service import BuildResult, TechnicalKnowledgeService
from backend.services.marketplace_catalog import get_catalog_cache

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class KnowledgeSelection(BaseModel):
    brand: str
    model: str
    generation_id: str | None = None
    engine_id: str | None = None
    modification_id: str | None = None


def _identity(selection: KnowledgeSelection) -> KnowledgeIdentity:
    try:
        return resolve_knowledge_identity(
            get_catalog_cache(),
            selection.brand,
            selection.model,
            selection.generation_id,
            selection.engine_id,
            selection.modification_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def get_technical_service() -> TechnicalKnowledgeService:
    settings = get_settings()
    grabber = TechnicalGrabber(
        providers=[
            WebSearchDiscovery(
                max_queries=settings.knowledge_max_search_queries,
                results_per_query=settings.knowledge_search_results_per_query,
                total_timeout_seconds=max(1.0, settings.knowledge_discovery_timeout_seconds - 2),
                backend="brave,duckduckgo,mojeek",
                name="ddgs_primary",
            ),
            WebSearchDiscovery(
                max_queries=settings.knowledge_max_search_queries,
                results_per_query=settings.knowledge_search_results_per_query,
                total_timeout_seconds=max(1.0, settings.knowledge_discovery_timeout_seconds - 2),
                backend="google,startpage,yahoo",
                name="ddgs_secondary",
            ),
            WikipediaDiscovery(),
        ],
        max_sources=settings.knowledge_max_sources,
        per_domain=settings.knowledge_max_sources_per_domain,
        timeout_seconds=settings.knowledge_source_timeout_seconds,
        total_discovery_timeout_seconds=settings.knowledge_discovery_timeout_seconds,
    )
    return TechnicalKnowledgeService(grabber=grabber, reranker=LocalCrossEncoderReranker())


def _response(result: BuildResult) -> dict[str, object]:
    scope = json.loads(result.scope_key) if result.scope_key else []
    scope_level = (
        "modification"
        if len(scope) > 4 and scope[4]
        else "engine"
        if len(scope) > 3 and scope[3]
        else "generation"
        if len(scope) > 2 and scope[2]
        else "model"
    )
    return runtime_timing(
        {
            "status": result.status,
            "phase": result.status,
            "build_id": result.build_id,
            "scope_key": result.scope_key,
            "scope_level": scope_level,
            "profile": result.profile.model_dump() if result.profile else None,
            "diagnostics": asdict(result.diagnostics),
            **asdict(result.diagnostics),
        }
    )


def _cached_response(result: BuildResult, identity: KnowledgeIdentity) -> dict[str, object]:
    response = _response(result)
    if result.scope_key != identity.scope_key:
        response["status"] = "fallback_cached"
        response["phase"] = "fallback_cached"
    return response


class KnowledgeJobs:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._results: dict[str, BuildResult] = {}
        self._errors: dict[str, str] = {}
        self._progress: dict[str, dict[str, object]] = {}
        self._started: dict[str, float] = {}

    def status(self, key: str) -> dict[str, object] | None:
        task = self._tasks.get(key)
        if task and not task.done():
            progress = dict(self._progress[key])
            progress["elapsed_ms"] = int((perf_counter() - self._started[key]) * 1000)
            return runtime_timing(progress)
        if key in self._results:
            return _response(self._results[key])
        if key in self._errors:
            failed = dict(self._progress.get(key, {}))
            failed.update(
                status="failed",
                phase="failed",
                scope_key=key,
                error_type=self._errors[key],
            )
            return failed
        return None

    def start(
        self,
        identity: KnowledgeIdentity,
        service: TechnicalKnowledgeService,
        *,
        force: bool = False,
        refresh_sources: bool = False,
    ) -> bool:
        key = identity.scope_key
        if key in self._tasks and not self._tasks[key].done():
            return False
        self._results.pop(key, None)
        self._errors.pop(key, None)
        self._started[key] = perf_counter()
        self._progress[key] = {
            "status": "building",
            "phase": "queued",
            "scope_key": key,
            "started_at": datetime.now(UTC).isoformat(),
            "elapsed_ms": 0,
        }

        def update_progress(phase, diagnostics) -> None:
            now = datetime.now(UTC).isoformat()
            started_at = self._progress[key]["started_at"]
            if self._progress[key].get("phase") != phase:
                self._progress[key]["phase_started_at"] = now
            self._progress[key].update(asdict(diagnostics))
            self._progress[key]["updated_at"] = diagnostics.updated_at or now
            self._progress[key]["started_at"] = diagnostics.started_at or started_at
            self._progress[key]["phase_started_at"] = diagnostics.phase_started_at or now
            self._progress[key]["phase"] = phase
            self._progress[key]["elapsed_ms"] = int((perf_counter() - self._started[key]) * 1000)

        async def run() -> None:
            try:
                self._results[key] = await service.build(
                    identity,
                    force=force,
                    refresh_sources=refresh_sources,
                    progress=update_progress,
                )
            except Exception as exc:
                self._errors[key] = type(exc).__name__
                self._progress[key].update(phase="failed", last_error=type(exc).__name__)
            except asyncio.CancelledError:
                self._errors[key] = "BuildInterrupted"
                self._progress[key].update(phase="failed", last_error="BuildInterrupted")
                raise

        self._tasks[key] = asyncio.create_task(run())
        return True


jobs = KnowledgeJobs()


@router.get("/config")
async def knowledge_config(
    service: Annotated[TechnicalKnowledgeService, Depends(get_technical_service)],
) -> dict[str, bool]:
    return {"provider_configured": service.generator.configured}


@router.get("/profile")
async def knowledge_profile(
    selection: Annotated[KnowledgeSelection, Depends()],
    service: Annotated[TechnicalKnowledgeService, Depends(get_technical_service)],
) -> dict[str, object]:
    identity = _identity(selection)
    cached = service.cached(identity)
    if cached:
        return _cached_response(cached, identity)
    return {
        "status": "provider_not_configured" if not service.generator.configured else "not_found",
        "scope_key": identity.scope_key,
        "profile": None,
    }


@router.post("/build")
async def knowledge_build(
    selection: KnowledgeSelection,
    service: Annotated[TechnicalKnowledgeService, Depends(get_technical_service)],
) -> dict[str, object]:
    identity = _identity(selection)
    cached = service.cached(identity)
    if cached and cached.scope_key == identity.scope_key:
        return _response(cached)
    if not service.generator.configured:
        return {"status": "provider_not_configured", "scope_key": identity.scope_key}
    jobs.start(identity, service)
    return {"status": "building", "phase": "queued", "scope_key": identity.scope_key}


@router.post("/rebuild")
async def knowledge_rebuild(
    selection: KnowledgeSelection,
    service: Annotated[TechnicalKnowledgeService, Depends(get_technical_service)],
    refresh_sources: bool = False,
) -> dict[str, object]:
    identity = _identity(selection)
    if not service.generator.configured:
        return {"status": "provider_not_configured", "scope_key": identity.scope_key}
    jobs.start(identity, service, force=True, refresh_sources=refresh_sources)
    return {"status": "building", "phase": "queued", "scope_key": identity.scope_key}


@router.get("/status")
async def knowledge_status(
    selection: Annotated[KnowledgeSelection, Depends()],
    service: Annotated[TechnicalKnowledgeService, Depends(get_technical_service)],
) -> dict[str, object]:
    identity = _identity(selection)
    status = jobs.status(identity.scope_key)
    if status:
        return status
    persisted = service.build_status(identity)
    cached = service.cached(identity)
    if persisted:
        if cached and persisted.get("profile_persisted"):
            persisted["profile"] = cached.profile.model_dump()
            persisted["scope_level"] = _response(cached)["scope_level"]
        return persisted
    return (
        _cached_response(cached, identity)
        if cached
        else {
            "status": "not_found",
            "scope_key": identity.scope_key,
        }
    )


@router.get("/sources")
async def knowledge_sources(
    selection: Annotated[KnowledgeSelection, Depends()],
    service: Annotated[TechnicalKnowledgeService, Depends(get_technical_service)],
) -> dict[str, object]:
    identity = _identity(selection)
    return {
        "scope_key": identity.scope_key,
        "sources": service.sources(identity),
        "discovery_attempts": service.discovery_attempts(identity),
    }
