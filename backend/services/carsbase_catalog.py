"""CarsBase inventory import. Never fetches from customer-facing catalog reads."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from backend.models.catalog import CatalogBrand, CatalogModel, SourceReference
from backend.services.marketplace_catalog import (
    _CATALOG_WRITE_LOCK,
    CatalogCache,
    _brand_name,
    _catalog_file_lock,
    _identity_keys,
    _key,
    _model_key,
)

SOURCE = "cars-base.ru"
BASE_URL = "https://api.cars-base.ru"
FULL_URL = f"{BASE_URL}/full"


class CarsBaseError(ValueError):
    def __init__(self, reason: str, retry_after_seconds: int | None = None) -> None:
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class CarsBaseModel:
    external_id: str
    brand_external_id: str
    name: str
    metadata: dict[str, str | int | bool | None]


@dataclass(frozen=True, slots=True)
class CarsBaseBrand:
    external_id: str
    name: str
    metadata: dict[str, str | int | bool | None]
    models: tuple[CarsBaseModel, ...]


@dataclass(frozen=True, slots=True)
class CarsBaseSnapshot:
    brands: tuple[CarsBaseBrand, ...]
    model_count: int


def _external_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CarsBaseError("invalid_external_id")
    result = str(value).strip()
    if not result or len(result) > 120:
        raise CarsBaseError("invalid_external_id")
    return result


def _metadata(
    record: dict[str, Any], fields: tuple[str, ...]
) -> dict[str, str | int | bool | None]:
    metadata: dict[str, str | int | bool | None] = {}
    for field in fields:
        value = record.get(field)
        if value is not None and not isinstance(value, (str, int, bool)):
            raise CarsBaseError(f"invalid_{field}")
        metadata[field] = value
    for year in ("year_from", "year_to"):
        value = metadata.get(year)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 1800 <= value <= 2200
        ):
            raise CarsBaseError(f"invalid_{year}")
    first, last = metadata.get("year_from"), metadata.get("year_to")
    if first is not None and last is not None and first > last:
        raise CarsBaseError("invalid_year_range")
    return metadata


def parse_status(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("last_update"), str):
        raise CarsBaseError("invalid_status")
    version = value["last_update"].strip()
    try:
        datetime.fromisoformat(version.replace(" ", "T"))
    except ValueError as exc:
        raise CarsBaseError("invalid_last_update") from exc
    counts = value.get("counts")
    if not isinstance(counts, dict) or any(
        isinstance(counts.get(key), bool) or not isinstance(counts.get(key), int) or counts[key] < 1
        for key in ("marks", "models")
    ):
        raise CarsBaseError("invalid_status_counts")
    return {"last_update": version, "brand_count": counts["marks"], "model_count": counts["models"]}


def parse_full(value: object, status: dict[str, object] | None = None) -> CarsBaseSnapshot:
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        raise CarsBaseError("invalid_full")
    records = value["data"]
    if not records:
        raise CarsBaseError("empty_full")
    brands: list[CarsBaseBrand] = []
    brand_ids: set[str] = set()
    model_ids: set[str] = set()
    for raw_brand in records:
        if not isinstance(raw_brand, dict) or not isinstance(raw_brand.get("models"), list):
            raise CarsBaseError("invalid_brand")
        brand_id = _external_id(raw_brand.get("id"))
        name = raw_brand.get("name")
        if brand_id in brand_ids or not isinstance(name, str) or not name.strip():
            raise CarsBaseError("duplicate_or_invalid_brand")
        brand_ids.add(brand_id)
        brand_metadata = _metadata(
            raw_brand,
            (
                "cyrillic_name",
                "country",
                "year_from",
                "year_to",
                "popular",
                "numeric_id",
                "updated_at",
            ),
        )
        models: list[CarsBaseModel] = []
        for raw_model in raw_brand["models"]:
            if not isinstance(raw_model, dict):
                raise CarsBaseError("invalid_model")
            model_id = _external_id(raw_model.get("id"))
            model_name = raw_model.get("name")
            if model_id in model_ids or not isinstance(model_name, str) or not model_name.strip():
                raise CarsBaseError("duplicate_or_invalid_model")
            if _external_id(raw_model.get("mark_id")) != brand_id:
                raise CarsBaseError("model_brand_id_mismatch")
            model_ids.add(model_id)
            models.append(
                CarsBaseModel(
                    model_id,
                    brand_id,
                    model_name.strip(),
                    _metadata(
                        raw_model,
                        ("cyrillic_name", "year_from", "year_to", "class", "updated_at"),
                    ),
                )
            )
        brands.append(CarsBaseBrand(brand_id, name.strip(), brand_metadata, tuple(models)))
    count = len(model_ids)
    if not count or (
        status and (len(brands), count) != (status["brand_count"], status["model_count"])
    ):
        raise CarsBaseError("truncated_or_inconsistent_full")
    return CarsBaseSnapshot(tuple(brands), count)


class CarsBaseClient:
    def __init__(self, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        self.timeout = timeout
        self.transport = transport

    async def _get(self, path: str) -> object:
        limit = 1_000_000 if path == "status" else 16_000_000
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport, follow_redirects=False
            ) as client, client.stream("GET", f"{BASE_URL}/{path}") as response:
                if response.status_code == 429:
                    raw = response.headers.get("Retry-After", "")
                    delay = min(7200, max(1, int(raw))) if raw.isdigit() else None
                    raise CarsBaseError("rate_limited", delay)
                if response.status_code != 200:
                    raise CarsBaseError(f"http_{response.status_code}")
                content_length = response.headers.get("Content-Length", "")
                if content_length.isdigit() and int(content_length) > limit:
                    raise CarsBaseError("oversized_response")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise CarsBaseError("oversized_response")
        except httpx.HTTPError as exc:
            raise CarsBaseError(f"network_{type(exc).__name__}") from exc
        try:
            return json.loads(body)
        except ValueError as exc:
            raise CarsBaseError("invalid_json") from exc

    async def status(self) -> dict[str, object]:
        return parse_status(await self._get("status"))

    async def full(self, status: dict[str, object] | None = None) -> CarsBaseSnapshot:
        return parse_full(await self._get("full"), status)


def _reference(external_id: str) -> dict[str, str]:
    return SourceReference(
        source=SOURCE, url=FULL_URL, path="/full", slug="", external_id=external_id
    ).model_dump(mode="json")


def _ref_id(entry: dict) -> str | None:
    return next(
        (
            str(ref["external_id"])
            for ref in entry.get("source_refs", [])
            if ref.get("source") == SOURCE and ref.get("external_id")
        ),
        None,
    )


def _stable_id(base: str, external_id: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    suffix = hashlib.sha256(external_id.encode()).hexdigest()[:10]
    candidate = f"{base}-{suffix}"
    if candidate in used:
        raise CarsBaseError("canonical_id_collision")
    used.add(candidate)
    return candidate


def _attach_ref(entry: dict, external_id: str) -> None:
    refs = [ref for ref in entry.get("source_refs", []) if ref.get("source") != SOURCE]
    entry["source_refs"] = [*refs, _reference(external_id)]


def reconcile(
    existing: dict[str, object], snapshot: CarsBaseSnapshot
) -> tuple[dict[str, object], dict]:
    """Pure, additive comparison: exact external IDs first; never overwrite enrichment."""
    payload = deepcopy(existing)
    brands: list[dict] = payload.setdefault("brands", [])  # type: ignore[assignment]
    existing_model_ids = {str(m["id"]) for b in brands for m in b.get("models", [])}
    used_brand_ids = {str(b["id"]) for b in brands}
    used_model_ids = set(existing_model_ids)
    upstream_model_ids = {model.external_id for brand in snapshot.brands for model in brand.models}
    matched_existing: set[str] = set()
    report: dict[str, object] = {
        "current_brand_count": len(brands),
        "current_model_count": len(existing_model_ids),
        "carsbase_brand_count": len(snapshot.brands),
        "carsbase_model_count": snapshot.model_count,
        "exact_matched_models": 0,
        "models_added": 0,
        "ambiguous_mappings": [],
        "alias_collisions": [],
        "external_id_collisions": [],
        "year_range_conflicts": [],
        "class_segment_conflicts": [],
    }
    for incoming_brand in snapshot.brands:
        candidates = [b for b in brands if _ref_id(b) == incoming_brand.external_id]
        if not candidates:
            key = _key(_brand_name(incoming_brand.name))
            candidates = [
                b
                for b in brands
                if str(b.get("id")) == key
                or _key(_brand_name(str(b.get("name", "")))) == key
                or any(_key(_brand_name(str(a))) == key for a in b.get("aliases", []))
            ]
            candidates = [b for b in candidates if _ref_id(b) in (None, incoming_brand.external_id)]
        if len(candidates) == 1:
            brand = candidates[0]
        else:
            if candidates:
                report["ambiguous_mappings"].append(
                    {"brand_external_id": incoming_brand.external_id}
                )
            brand_id = _stable_id(
                _key(_brand_name(incoming_brand.name)) or "brand",
                incoming_brand.external_id,
                used_brand_ids,
            )
            brand = CatalogBrand(id=brand_id, name=incoming_brand.name).model_dump(mode="json")
            brands.append(brand)
        _attach_ref(brand, incoming_brand.external_id)
        brand["carsbase_metadata"] = incoming_brand.metadata
        if incoming_brand.name != brand["name"] and incoming_brand.name not in brand.get(
            "aliases", []
        ):
            brand.setdefault("aliases", []).append(incoming_brand.name)
        models = brand.setdefault("models", [])
        for incoming in incoming_brand.models:
            by_id = [m for m in models if _ref_id(m) == incoming.external_id]
            if len(by_id) > 1:
                report["ambiguous_mappings"].append(
                    {
                        "brand_external_id": incoming_brand.external_id,
                        "model_external_id": incoming.external_id,
                        "candidate_ids": [str(m["id"]) for m in by_id],
                    }
                )
                continue
            if by_id:
                candidates = by_id
            else:
                exact_id = f"{brand['id']}:{_model_key(incoming.name)}"
                candidates = [m for m in models if str(m["id"]) == exact_id]
                if not candidates:
                    exact_name = _model_key(incoming.name)
                    candidates = [m for m in models if _model_key(str(m["name"])) == exact_name]
                if not candidates:
                    keys = _identity_keys(incoming.name)
                    candidates = [
                        m
                        for m in models
                        if keys
                        & set().union(
                            *(
                                _identity_keys(str(value))
                                for value in [m["name"], *m.get("aliases", [])]
                            )
                        )
                    ]
                claimed = [m for m in candidates if _ref_id(m) not in (None, incoming.external_id)]
                if claimed:
                    report["ambiguous_mappings"].append(
                        {
                            "brand_external_id": incoming_brand.external_id,
                            "model_external_id": incoming.external_id,
                            "candidate_ids": [str(m["id"]) for m in claimed],
                        }
                    )
                candidates = [m for m in candidates if _ref_id(m) in (None, incoming.external_id)]
            if len(candidates) == 1 and str(candidates[0]["id"]) not in matched_existing:
                model = candidates[0]
                if str(model["id"]) in existing_model_ids:
                    matched_existing.add(str(model["id"]))
                    report["exact_matched_models"] += 1
            else:
                if candidates:
                    report["ambiguous_mappings"].append(
                        {
                            "brand_external_id": incoming_brand.external_id,
                            "model_external_id": incoming.external_id,
                            "candidate_ids": [str(m["id"]) for m in candidates],
                        }
                    )
                model_id = _stable_id(
                    f"{brand['id']}:{_model_key(incoming.name) or 'model'}",
                    incoming.external_id,
                    used_model_ids,
                )
                model = CatalogModel(id=model_id, name=incoming.name).model_dump(mode="json")
                models.append(model)
                report["models_added"] += 1
            previous_meta = model.get("carsbase_metadata") or {}
            for field in ("year_from", "year_to"):
                previous = previous_meta.get(field)
                current = incoming.metadata.get(field)
                if previous is not None and current is not None and previous != current:
                    report["year_range_conflicts"].append({"model_id": model["id"], "field": field})
            for generation in model.get("generations", []):
                start = generation.get("year_from")
                end = generation.get("year_to")
                model_start = incoming.metadata.get("year_from")
                model_end = incoming.metadata.get("year_to")
                if (
                    isinstance(start, int)
                    and isinstance(model_start, int)
                    and start < model_start
                    or isinstance(end, int)
                    and isinstance(model_end, int)
                    and end > model_end
                ):
                    report["year_range_conflicts"].append(
                        {"model_id": model["id"], "generation_id": generation.get("id")}
                    )
            existing_code = str(
                (model.get("classification") or {}).get("segment_code") or ""
            ).upper()
            new_class = str(incoming.metadata.get("class") or "").upper()
            if (
                existing_code
                and new_class
                and existing_code != new_class
                and not (new_class == "J" and existing_code.startswith("J-"))
            ):
                report["class_segment_conflicts"].append(
                    {"model_id": model["id"], "class": new_class}
                )
            _attach_ref(model, incoming.external_id)
            model["carsbase_metadata"] = incoming.metadata
            if incoming.name != model["name"] and incoming.name not in model.get("aliases", []):
                model.setdefault("aliases", []).append(incoming.name)
    report["models_only_in_current"] = len(existing_model_ids - matched_existing)
    report["models_only_in_carsbase"] = report["models_added"]
    external_owners: dict[str, str] = {}
    for brand in brands:
        alias_owners: dict[str, str] = {}
        for model in brand.get("models", []):
            owner_id = str(model["id"])
            external_id = _ref_id(model)
            if external_id:
                other = external_owners.setdefault(external_id, owner_id)
                if other != owner_id:
                    report["external_id_collisions"].append(
                        {"external_id": external_id, "model_id": owner_id, "other": other}
                    )
            for value in [model["name"], *model.get("aliases", [])]:
                for key in _identity_keys(str(value)):
                    other = alias_owners.setdefault(key, owner_id)
                    if other != owner_id:
                        report["alias_collisions"].append(
                            {
                                "brand_id": brand["id"],
                                "key": key,
                                "model_id": owner_id,
                                "other": other,
                            }
                        )
    report["carsbase_source_ref_coverage"] = len(
        {
            external_id
            for brand in brands
            for model in brand.get("models", [])
            if (external_id := _ref_id(model)) in upstream_model_ids
        }
    )
    report["drom_source_ref_coverage_for_carsbase"] = len(
        {
            external_id
            for brand in brands
            for model in brand.get("models", [])
            if (external_id := _ref_id(model)) in upstream_model_ids
            and any(ref.get("source") == "drom.ru" for ref in model.get("source_refs", []))
        }
    )
    report["inventory_completeness_pct"] = round(
        100 * report["carsbase_source_ref_coverage"] / snapshot.model_count, 2
    )
    report["generation_enrichment_coverage"] = len(
        {
            external_id
            for brand in brands
            for model in brand.get("models", [])
            if (external_id := _ref_id(model)) in upstream_model_ids and model.get("generations")
        }
    )
    return payload, report


class CarsBaseSyncService:
    def __init__(self, cache: CatalogCache | None = None, client: CarsBaseClient | None = None):
        self.cache = cache or CatalogCache()
        self.client = client or CarsBaseClient()

    async def sync(self, *, force: bool = False, dry_run: bool = False) -> dict[str, object]:
        stored = self.cache.load().get("sources", {}).get(SOURCE, {})
        try:
            status = await self.client.status()
            if not force and not dry_run and stored.get("last_update") == status["last_update"]:
                return {
                    "status": "unchanged",
                    **status,
                    "last_successful_sync": stored.get("last_successful_sync"),
                }
            snapshot = await self.client.full(status)
            if dry_run:
                _, report = reconcile(self.cache.load(), snapshot)
                return {
                    "status": "dry_run",
                    "upstream_last_update": status["last_update"],
                    **report,
                }
            async with _CATALOG_WRITE_LOCK:
                with _catalog_file_lock(self.cache.write_path()):
                    self.cache._payload = None
                    payload, report = reconcile(self.cache.load(), snapshot)
                    now = datetime.now(UTC).isoformat()
                    payload["catalog_updated_at"] = now
                    payload.setdefault("sources", {})[SOURCE] = {
                        "status": "ok",
                        "last_update": status["last_update"],
                        "last_successful_sync": now,
                        "brand_count": len(snapshot.brands),
                        "model_count": snapshot.model_count,
                        "exact_matched_models": report["exact_matched_models"],
                        "models_added": report["models_added"],
                        "ambiguous_mappings": len(report["ambiguous_mappings"]),
                        "alias_collisions": len(report["alias_collisions"]),
                        "external_id_collisions": len(report["external_id_collisions"]),
                        "models_only_in_current": report["models_only_in_current"],
                        "source_ref_coverage": report["carsbase_source_ref_coverage"],
                    }
                    self.cache.save(payload)
            return {"status": "synced", "upstream_last_update": status["last_update"], **report}
        except CarsBaseError as exc:
            return {
                "status": "rate_limited" if exc.reason == "rate_limited" else "error",
                "reason": exc.reason,
                "retry_after_seconds": exc.retry_after_seconds,
                "last_good_preserved": True,
            }
