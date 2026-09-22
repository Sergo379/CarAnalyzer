"""Resumable, paced enrichment of every model in the active vehicle catalog.

Each model is atomically checkpointed in the ignored runtime catalog. Examples:
``python -m backend.tools.catalog_enrich --resume``
``python -m backend.tools.catalog_enrich --resume --retry-failed``
``python -m backend.tools.catalog_enrich --promote``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime

import httpx

from backend.services.marketplace_catalog import (
    CATALOG_PARSER_VERSION,
    CATALOG_SEED_PATH,
    USER_AGENT,
    CatalogCache,
    CatalogEnrichmentService,
)
from backend.tools.catalog_audit import audit_catalog


class PacedClient(httpx.AsyncClient):
    def __init__(self, pace_seconds: float) -> None:
        super().__init__(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )
        self.pace_seconds = pace_seconds
        self.last_request = 0.0
        self.request_count = 0
        self._pace_lock = asyncio.Lock()

    async def get(self, url: str, **kwargs) -> httpx.Response:  # type: ignore[override]
        async with self._pace_lock:
            delay = self.pace_seconds - (time.monotonic() - self.last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_request = time.monotonic()
            self.request_count += 1
            return await super().get(url, **kwargs)


def _eligible(entry: dict, retry_failed: bool) -> bool:
    status = entry.get("details_status", "not_checked")
    if (
        status in ("complete", "source_has_no_generation_data")
        and entry.get("details_parser_version") != CATALOG_PARSER_VERSION
    ):
        return True
    if status == "complete":
        return False
    if status == "not_checked":
        # Legacy seed entries may contain generations but were never status checked.
        return True
    if retry_failed and status != "rate_limited":
        return True
    retry_at = entry.get("details_retry_at")
    if not retry_at:
        return True
    return datetime.fromisoformat(str(retry_at)) <= datetime.now(UTC)


async def enrich_all(
    cache: CatalogCache,
    *,
    retry_failed: bool = False,
    pace_seconds: float = 1.5,
    max_models: int | None = None,
) -> dict:
    service = CatalogEnrichmentService(cache)
    model_ids = [
        (str(brand["name"]), str(model["name"]))
        for brand in cache.load().get("brands", [])
        for model in brand.get("models", [])
    ]
    outcomes: dict[str, int] = {}
    processed = 0
    consecutive_unavailable = 0
    async with PacedClient(pace_seconds) as client:
        for brand, model in model_ids:
            entry = cache.model_entry(brand, model)
            if entry is None or not _eligible(entry, retry_failed):
                continue
            if max_models is not None and processed >= max_models:
                break
            status = await service.enrich_model(brand, model, client, retry_failed=retry_failed)
            processed += 1
            outcomes[status] = outcomes.get(status, 0) + 1
            print(
                json.dumps(
                    {"checked": processed, "brand": brand, "model": model, "status": status},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if status == "rate_limited":
                # Keep the checkpoint and stop the source operation. An explicit
                # resume after Retry-After is safer than probing thousands of URLs.
                break
            latest = cache.model_entry(brand, model)
            if (
                status == "source_unavailable"
                and latest
                and latest.get("details_error_type") == "HTTP_403"
            ):
                break
            if status == "source_unavailable":
                consecutive_unavailable += 1
                if consecutive_unavailable >= 3:
                    break
                await asyncio.sleep(min(2**consecutive_unavailable, 30))
            else:
                consecutive_unavailable = 0
    return {
        "processed": processed,
        "http_requests": client.request_count,
        "outcomes": outcomes,
        "audit": audit_catalog(cache)["summary"],
    }


def promote() -> dict:
    runtime_path = CatalogCache().runtime_path
    if not runtime_path.exists():
        raise RuntimeError("No runtime catalog to promote")
    runtime = CatalogCache(path=runtime_path)
    report = audit_catalog(runtime)
    summary = report["summary"]
    unresolved = sum(
        summary.get(status, 0)
        for status in ("not_checked", "parse_error", "source_unavailable", "rate_limited")
    )
    if report["errors"] or unresolved:
        raise RuntimeError(
            f"Promotion blocked: {summary['validation_errors']} validation errors, "
            f"{unresolved} models unresolved"
        )
    CatalogCache(path=CATALOG_SEED_PATH).save(runtime.load())
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume", action="store_true", help="Resume from model status checkpoints (default)"
    )
    parser.add_argument(
        "--retry-failed", action="store_true", help="Retry negative statuses before their TTL"
    )
    parser.add_argument("--pace-seconds", type=float, default=1.5)
    parser.add_argument("--max-models", type=int)
    parser.add_argument(
        "--promote", action="store_true", help="Manually validate and promote runtime to seed"
    )
    args = parser.parse_args()
    if args.promote:
        report = promote()
    else:
        if args.pace_seconds < 0.5:
            parser.error("--pace-seconds must be at least 0.5")
        report = asyncio.run(
            enrich_all(
                CatalogCache(),
                retry_failed=args.retry_failed,
                pace_seconds=args.pace_seconds,
                max_models=args.max_models,
            )
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
