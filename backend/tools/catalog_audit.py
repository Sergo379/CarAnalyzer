"""Offline, catalog-wide integrity and coverage audit.

Run with ``python -m backend.tools.catalog_audit --json``. No marketplace
requests are made and the catalog is never modified.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from backend.models.catalog import CatalogBrand
from backend.services.marketplace_catalog import (
    CATALOG_PARSER_VERSION,
    CatalogCache,
    _identity_keys,
)

DETAIL_STATUSES = frozenset(
    {
        "complete",
        "source_has_no_generation_data",
        "parse_error",
        "source_unavailable",
        "rate_limited",
        "not_checked",
    }
)
PSEUDO_NAMES = frozenset(
    {"все модели", "все автомобили", "модель", "модели", "каталог", "catalog", "engine", "frame"}
)


def _valid_ref(ref: dict) -> bool:
    parsed = urlparse(str(ref.get("url", "")))
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.path == str(ref.get("path", ""))
        and bool(ref.get("slug"))
        and bool(ref.get("source"))
    )


def audit_catalog(
    cache: CatalogCache | None = None,
    source_model_counts: dict[str, dict[str, int]] | None = None,
) -> dict:
    """Audit stored models; optional index counts use canonical brand IDs.

    Index counts must come from an independent source-model snapshot. Without
    them, completeness is unknown rather than inferred from generation coverage.
    """
    payload = (cache or CatalogCache()).load()
    records: list[dict] = []
    errors: list[dict] = []
    per_brand: list[dict] = []
    brand_ids: set[str] = set()
    model_ids: set[str] = set()
    source_ref_owners: dict[tuple[str, str], str] = {}
    engine_keys: set[str] = set()
    counts: Counter[str] = Counter()
    index_counts = source_model_counts or {}
    compared_brands: set[str] = set()
    gap_brands: set[str] = set()

    for brand in payload.get("brands", []):
        brand_id = str(brand.get("id", ""))
        if not brand_id or brand_id in brand_ids:
            errors.append({"id": brand_id, "type": "canonical_brand_collision"})
        brand_ids.add(brand_id)
        try:
            CatalogBrand.model_validate(brand)
        except ValidationError as exc:
            errors.append({"id": brand_id, "type": "schema", "detail": str(exc)[:300]})

        brand_counts: Counter[str] = Counter()
        alias_owners: dict[str, str] = {}
        for model in brand.get("models", []):
            model_id = str(model.get("id", ""))
            if not model_id or model_id in model_ids:
                errors.append({"id": model_id, "type": "canonical_model_collision"})
            model_ids.add(model_id)
            if not model_id.startswith(f"{brand_id}:"):
                errors.append({"id": model_id, "type": "brand_model_id_mismatch"})

            refs = model.get("source_refs", [])
            if not refs:
                errors.append({"id": model_id, "type": "missing_source_ref"})
            for ref in refs:
                if not _valid_ref(ref):
                    errors.append({"id": model_id, "type": "invalid_source_ref", "ref": ref})
                source_key = (str(ref.get("source", "")), str(ref.get("url", "")))
                owner = source_ref_owners.setdefault(source_key, model_id)
                if owner != model_id:
                    errors.append(
                        {
                            "id": model_id,
                            "type": "source_id_collision",
                            "source": source_key[0],
                            "url": source_key[1],
                            "other": owner,
                        }
                    )

            name = str(model.get("name", ""))
            if name.casefold() in PSEUDO_NAMES or not name.strip():
                errors.append({"id": model_id, "type": "pseudo_model", "name": name})
            for value in [name, *model.get("aliases", [])]:
                for key in _identity_keys(str(value)):
                    owner = alias_owners.setdefault(key, model_id)
                    if owner != model_id:
                        errors.append(
                            {
                                "id": model_id,
                                "type": "alias_collision",
                                "alias": value,
                                "other": owner,
                            }
                        )

            generations = model.get("generations", [])
            modifications = [
                item for generation in generations for item in generation.get("modifications", [])
            ]
            engines = [item.get("engine") for item in modifications if item.get("engine")]
            bodies = sorted(
                {
                    *(
                        str(body)
                        for generation in generations
                        for body in generation.get("body_types", [])
                    ),
                    *(str(item["body_type"]) for item in modifications if item.get("body_type")),
                }
            )
            status = str(model.get("details_status", "not_checked"))
            if (
                status in ("complete", "source_has_no_generation_data")
                and model.get("details_parser_version") != CATALOG_PARSER_VERSION
            ):
                errors.append({"id": model_id, "type": "outdated_parser_version"})
                counts["needs_reparse"] += 1
                brand_counts["needs_reparse"] += 1
            elif status == "complete":
                counts["validated_complete"] += 1
                brand_counts["validated_complete"] += 1
            if status not in DETAIL_STATUSES:
                errors.append({"id": model_id, "type": "invalid_details_status", "status": status})
            if status == "complete" and not generations:
                errors.append({"id": model_id, "type": "complete_without_generations"})
            if generations and status not in ("complete", "not_checked"):
                errors.append({"id": model_id, "type": "generations_with_failure_status"})
            if generations and status == "not_checked":
                # Legacy seed data is visible in coverage but needs a checked status at promotion.
                errors.append({"id": model_id, "type": "legacy_generation_status"})

            generation_ids: set[str] = set()
            for generation in generations:
                gen_id = str(generation.get("id", ""))
                if not gen_id or gen_id in generation_ids:
                    errors.append(
                        {"id": model_id, "type": "generation_collision", "generation": gen_id}
                    )
                generation_ids.add(gen_id)
                first, last = generation.get("year_from"), generation.get("year_to")
                if first is not None and last is not None and int(first) > int(last):
                    errors.append(
                        {
                            "id": model_id,
                            "type": "invalid_generation_interval",
                            "generation": gen_id,
                        }
                    )
                modification_ids: set[str] = set()
                for item in generation.get("modifications", []):
                    mod_id = str(item.get("id", ""))
                    if not mod_id or mod_id in modification_ids:
                        errors.append(
                            {
                                "id": model_id,
                                "type": "modification_collision",
                                "modification": mod_id,
                            }
                        )
                    modification_ids.add(mod_id)
                    if item.get("engine"):
                        engine_keys.add(CatalogCache.engine_key(item))

            record = {
                "canonical_brand_id": brand_id,
                "canonical_model_id": model_id,
                "brand": brand.get("name"),
                "model": name,
                "source_mappings": refs,
                "generation_count": len(generations),
                "modification_count": len(modifications),
                "engine_count": len(engines),
                "body_types": bodies,
                "details_status": status,
                "details_parser_version": model.get("details_parser_version"),
                "last_checked": model.get("details_checked_at"),
                "error_type": model.get("details_error_type"),
                "error_detail": model.get("details_error_detail"),
            }
            records.append(record)
            brand_counts["models"] += 1
            counts["models"] += 1
            for key, present in (
                ("source_refs", bool(refs)),
                ("generations", bool(generations)),
                ("modifications", bool(modifications)),
                ("engines", bool(engines)),
                ("body_information", bool(bodies)),
            ):
                if present:
                    counts[f"models_with_{key}"] += 1
                    brand_counts[f"models_with_{key}"] += 1
            counts["generations"] += len(generations)
            counts["modifications"] += len(modifications)
            counts[status] += 1
            brand_counts[status] += 1

        completeness: dict[str, dict[str, int | float]] = {}
        for source, brand_index in index_counts.items():
            if brand_id not in brand_index:
                continue
            expected = brand_index[brand_id]
            if not isinstance(expected, int) or expected < 0:
                raise ValueError("Source model counts must be non-negative integers")
            stored_models = sum(
                any(ref.get("source") == source for ref in model.get("source_refs", []))
                for model in brand.get("models", [])
            )
            stored_refs = len(
                {
                    str(ref.get("url"))
                    for model in brand.get("models", [])
                    for ref in model.get("source_refs", [])
                    if ref.get("source") == source
                }
            )
            missing = max(0, expected - stored_refs)
            collapsed = max(0, stored_refs - stored_models)
            completeness[source] = {
                "source_model_count": expected,
                "stored_model_count": stored_models,
                "stored_source_ref_count": stored_refs,
                "missing_model_count": missing,
                "canonical_alias_collapses": collapsed,
                "coverage_pct": round(100 * stored_refs / expected, 2) if expected else 100,
            }
            counts["source_index_comparisons"] += 1
            counts["source_index_missing_mappings"] += missing
            counts["source_index_canonical_alias_collapses"] += collapsed
            compared_brands.add(brand_id)
            if missing:
                gap_brands.add(brand_id)
                errors.append(
                    {
                        "id": brand_id,
                        "type": "source_model_count_gap",
                        "source": source,
                        **completeness[source],
                    }
                )

        total = brand_counts["models"]
        per_brand.append(
            {
                "id": brand_id,
                "name": brand.get("name"),
                **dict(brand_counts),
                "source_model_completeness": completeness,
                "pending": brand_counts["not_checked"] + brand_counts["needs_reparse"],
                "generation_coverage_pct": round(
                    100 * brand_counts["models_with_generations"] / total, 2
                )
                if total
                else 0,
                "engine_coverage_pct": round(100 * brand_counts["models_with_engines"] / total, 2)
                if total
                else 0,
            }
        )

    for source, brand_index in index_counts.items():
        for brand_id, expected in brand_index.items():
            if brand_id in brand_ids:
                continue
            if not isinstance(expected, int) or expected < 0:
                raise ValueError("Source model counts must be non-negative integers")
            compared_brands.add(brand_id)
            counts["source_index_comparisons"] += 1
            counts["source_index_missing_mappings"] += expected
            if expected:
                gap_brands.add(brand_id)
                errors.append(
                    {
                        "id": brand_id,
                        "type": "source_brand_missing",
                        "source": source,
                        "source_model_count": expected,
                        "stored_model_count": 0,
                        "missing_model_count": expected,
                    }
                )

    total = counts["models"]
    summary = {
        "total_brands": len(payload.get("brands", [])),
        "total_models": total,
        "models_audited": len(records),
        **dict(counts),
        "pending": counts["not_checked"] + counts["needs_reparse"],
        "models_without_source_refs": total - counts["models_with_source_refs"],
        "models_without_generations": total - counts["models_with_generations"],
        "unique_engines": len(engine_keys),
        "source_index_available": bool(index_counts),
        "source_index_comparisons": counts["source_index_comparisons"],
        "source_index_brands_compared": len(compared_brands),
        "source_index_brands_with_gaps": len(gap_brands),
        "source_index_missing_mappings": counts["source_index_missing_mappings"],
        "source_index_canonical_alias_collapses": counts["source_index_canonical_alias_collapses"],
        "generation_coverage_pct": round(100 * counts["models_with_generations"] / total, 2)
        if total
        else 0,
        "engine_coverage_pct": round(100 * counts["models_with_engines"] / total, 2)
        if total
        else 0,
        "source_coverage_pct": round(100 * counts["models_with_source_refs"] / total, 2)
        if total
        else 0,
        "validation_errors": len(errors),
    }
    return {"summary": summary, "per_brand": per_brand, "records": records, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path", type=Path, help="Catalog JSON to audit; defaults to active runtime/seed"
    )
    parser.add_argument("--json", action="store_true", help="Print full machine-readable report")
    parser.add_argument(
        "--source-counts",
        type=Path,
        help='Optional independent index counts JSON: {"source": {"canonical_brand_id": count}}',
    )
    parser.add_argument(
        "--output", type=Path, help="Write full audit records and per-brand coverage"
    )
    args = parser.parse_args()
    source_counts = (
        json.loads(args.source_counts.read_text(encoding="utf-8")) if args.source_counts else None
    )
    report = audit_catalog(
        CatalogCache(path=args.path) if args.path else None,
        source_model_counts=source_counts,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report if args.json else report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
