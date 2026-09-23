"""Merge verified public index identities/refs into runtime, never enrichment data."""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse

from backend.models.catalog import CatalogBrand, CatalogModel
from backend.services.marketplace_catalog import (
    CatalogCache,
    _identity_keys,
    _model_key,
    _source_ref,
    auto_ru_source,
    drom_source,
)
from backend.tools.catalog_audit import PSEUDO_NAMES, audit_catalog


def merge_index_snapshot(payload: dict, snapshot: dict) -> tuple[dict, dict]:
    """Add source mappings and missing identities; preserve existing models verbatim."""
    updated = deepcopy(payload)
    source_name = snapshot["source"]
    source = drom_source() if source_name == "drom.ru" else auto_ru_source()
    brands = {str(brand["id"]): brand for brand in updated.get("brands", [])}
    added_brands = 0
    added_models = 0
    added_refs = 0
    skipped_ambiguous: list[dict] = []
    for source_brand in snapshot["brands"]:
        if "error" in source_brand:
            continue
        if source_brand["brand"].casefold() in source.excluded_brand_names:
            continue
        source_models = [
            model
            for model in source_brand.get("models", [])
            if model["name"].casefold() not in PSEUDO_NAMES
            and urlparse(model["url"]).path.rstrip("/").split("/")[-1].casefold()
            not in source.excluded_model_slugs
        ]
        if not source_models:
            continue
        brand_id = source_brand["brand_id"]
        brand = brands.get(brand_id)
        if brand is None:
            brand = CatalogBrand(
                id=brand_id,
                name=source_brand["brand"],
                source_refs=[_source_ref(source_name, source_brand["brand_url"])],
            ).model_dump(mode="json")
            brands[brand_id] = brand
            added_brands += 1
        else:
            brand_ref = _source_ref(source_name, source_brand["brand_url"]).model_dump(mode="json")
            if not any(
                ref.get("source") == source_name and ref.get("url") == brand_ref["url"]
                for ref in brand.get("source_refs", [])
            ):
                brand.setdefault("source_refs", []).append(brand_ref)
        models = brand.setdefault("models", [])
        used_ids = {str(model["id"]) for model in models}
        for source_model in source_models:
            url = source_model["url"]
            if any(
                ref.get("source") == source_name and ref.get("url") == url
                for model in models
                for ref in model.get("source_refs", [])
            ):
                continue
            name = source_model["name"]
            key = _model_key(name)
            name_keys = _identity_keys(name)
            matches = [
                model
                for model in models
                if any(
                    name_keys & _identity_keys(str(value))
                    for value in [model["name"], *model.get("aliases", [])]
                )
            ]
            if len(matches) > 1:
                skipped_ambiguous.append({"brand": brand_id, "model": name, "url": url})
                continue
            ref = _source_ref(source_name, url).model_dump(mode="json")
            if matches:
                matches[0].setdefault("source_refs", []).append(ref)
                added_refs += 1
                continue
            model_id = f"{brand_id}:{key}"
            if model_id in used_ids:
                digest = hashlib.sha256(url.encode()).hexdigest()[:8]
                model_id = f"{model_id}_{digest}"
            if model_id in used_ids:
                skipped_ambiguous.append({"brand": brand_id, "model": name, "url": url})
                continue
            models.append(
                CatalogModel(id=model_id, name=name, source_refs=[ref]).model_dump(mode="json")
            )
            used_ids.add(model_id)
            added_models += 1
    for brand in brands.values():
        brand["models"] = sorted(
            brand.get("models", []), key=lambda model: str(model["name"]).casefold()
        )
    updated["brands"] = sorted(brands.values(), key=lambda brand: str(brand["name"]).casefold())
    return updated, {
        "source": source_name,
        "added_brands": added_brands,
        "added_models": added_models,
        "added_refs": added_refs,
        "skipped_ambiguous": skipped_ambiguous,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, nargs="+")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    cache = CatalogCache()
    if args.apply and cache.write_path() == cache.seed_path:
        parser.error("Refusing to write the tracked seed")
    payload = cache.load()
    outcomes = []
    for path in args.snapshot:
        raw = path.read_bytes()
        try:
            decoded = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            # One pre-UTF-8 index snapshot was emitted using the Windows locale.
            decoded = raw.decode("cp1251")
        snapshot = json.loads(decoded)["snapshot"]
        payload, outcome = merge_index_snapshot(payload, snapshot)
        outcomes.append(outcome)
    report = audit_catalog(type("MemoryCache", (), {"load": lambda self: payload})())
    summary = report["summary"]
    if summary["validation_errors"] or any(outcome["skipped_ambiguous"] for outcome in outcomes):
        print(json.dumps({"outcomes": outcomes, "audit": summary}, ensure_ascii=False, indent=2))
        parser.error("Merge requires review; runtime was not changed")
    if args.apply:
        cache.save(payload)
    print(
        json.dumps(
            {"applied": args.apply, "outcomes": outcomes, "audit": summary},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
