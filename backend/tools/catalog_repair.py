"""Repair unambiguous same-source model collisions without network access.

Dry-run by default. ``--apply`` changes only the active runtime catalog; it
never promotes data to the tracked seed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from copy import deepcopy

from backend.models.catalog import CatalogModel
from backend.services.marketplace_catalog import CatalogCache, _model_key


def split_source_collisions(payload: dict) -> tuple[dict, list[dict]]:
    """Split aliases with their own source paths; leave ambiguous cases intact."""
    updated = deepcopy(payload)
    changes: list[dict] = []
    for brand in updated.get("brands", []):
        used_ids = {str(model["id"]) for model in brand.get("models", [])}
        additions: list[dict] = []
        for model in brand.get("models", []):
            refs = model.get("source_refs", [])
            if not any(n > 1 for n in Counter(ref["source"] for ref in refs).values()):
                continue
            base_key = _model_key(str(model["name"]))
            for alias in list(model.get("aliases", [])):
                alias_key = _model_key(str(alias))
                if not alias_key or alias_key == base_key:
                    continue
                alias_refs = [
                    ref for ref in refs if _model_key(str(ref.get("slug", ""))) == alias_key
                ]
                if not alias_refs or not any(
                    _model_key(str(ref.get("slug", ""))) == base_key for ref in refs
                ):
                    continue
                remaining_refs = [ref for ref in refs if ref not in alias_refs]
                if not remaining_refs:
                    continue
                new_id = f"{brand['id']}:{alias_key}"
                if new_id in used_ids:
                    digest = hashlib.sha256(str(alias_refs[0].get("url", "")).encode()).hexdigest()[
                        :8
                    ]
                    new_id = f"{new_id}_{digest}"
                if new_id in used_ids:
                    continue

                prefixes = tuple(str(ref["path"]).rstrip("/") + "/" for ref in alias_refs)
                moved_generations = []
                for generation in model.get("generations", []):
                    generation_paths = [
                        str(ref.get("path", "")) for ref in generation.get("source_refs", [])
                    ]
                    if generation_paths and all(
                        path.startswith(prefixes) for path in generation_paths
                    ):
                        moved_generations.append(generation)
                model["source_refs"] = remaining_refs
                model["aliases"].remove(alias)
                model["generations"] = [
                    generation
                    for generation in model.get("generations", [])
                    if generation not in moved_generations
                ]
                new_model = CatalogModel(
                    id=new_id,
                    name=alias,
                    source_refs=alias_refs,
                    generations=moved_generations,
                    details_status="complete" if moved_generations else "not_checked",
                    details_parser_version=(
                        model.get("details_parser_version") if moved_generations else None
                    ),
                    details_updated_at=(
                        model.get("details_updated_at") if moved_generations else None
                    ),
                    details_checked_at=(
                        model.get("details_checked_at") if moved_generations else None
                    ),
                ).model_dump(mode="json")
                additions.append(new_model)
                used_ids.add(new_id)
                changes.append(
                    {
                        "brand": brand["name"],
                        "from_id": model["id"],
                        "new_id": new_id,
                        "name": alias,
                        "source_refs": len(alias_refs),
                        "generations_moved": len(moved_generations),
                    }
                )
                refs = remaining_refs
        brand["models"] = sorted(
            [*brand.get("models", []), *additions], key=lambda item: str(item["name"]).casefold()
        )
    return updated, changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Save verified splits to runtime")
    args = parser.parse_args()
    cache = CatalogCache()
    if args.apply and cache.write_path() == cache.seed_path:
        parser.error("Refusing to write the tracked seed")
    updated, changes = split_source_collisions(cache.load())
    if args.apply and changes:
        cache.save(updated)
    print(json.dumps({"applied": args.apply, "changes": changes}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
