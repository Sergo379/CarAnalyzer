"""Small, data-selected live search smoke; never a marketplace sweep."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from backend.models.car import SearchRequest
from backend.services.marketplace_catalog import CatalogCache
from backend.services.search_service import SearchService


def select_cases(cache: CatalogCache | None = None) -> list[tuple[str, SearchRequest]]:
    """Choose common/older/newer catalog vehicles across brands and body types."""
    cache = cache or CatalogCache()
    candidates: list[dict] = []
    for brand in cache.load().get("brands", []):
        for model in brand.get("models", []):
            if model.get("details_status") != "complete":
                continue
            mapped = {ref["source"] for ref in model.get("source_refs", [])}
            if not {"auto.ru", "drom.ru"} <= mapped:
                continue
            for generation in model.get("generations", []):
                bodies = generation.get("body_types", [])
                first, last = generation.get("year_from"), generation.get("year_to")
                if not bodies or first is None:
                    continue
                candidates.append({
                    "brand": brand["name"],
                    "model": model["name"],
                    "body": bodies[0],
                    "year_from": int(first),
                    "year_to": int(last) if last is not None else None,
                    "generation_count": len(model["generations"]),
                    "brand_model_count": len(brand["models"]),
                })
    if not candidates:
        raise RuntimeError("No catalog vehicles meet the smoke-selection criteria")
    ranked = {
        "common": sorted(
            [item for item in candidates if item["year_to"] is None],
            key=lambda item: (
                -item["brand_model_count"], -item["generation_count"],
                item["brand"], item["model"],
            ),
        ),
        "older": sorted(
            [item for item in candidates if item["year_to"] and 2000 <= item["year_to"] <= 2015],
            key=lambda item: (
                -item["brand_model_count"], abs(item["year_to"] - 2010),
                item["brand"], item["model"],
            ),
        ),
        "newer": sorted(
            [item for item in candidates if item["year_from"] >= 2022],
            key=lambda item: (
                -item["brand_model_count"], abs(item["year_from"] - 2024),
                item["brand"], item["model"],
            ),
        ),
    }
    chosen: list[tuple[str, SearchRequest]] = []
    used_brands: set[str] = set()
    used_models: set[tuple[str, str]] = set()
    used_bodies: set[str] = set()
    for label in ("common", "older", "newer"):
        options = ranked[label]
        choice = next(
            (
                item for item in options
                if item["brand"] not in used_brands
                and (item["brand"], item["model"]) not in used_models
                and item["body"] not in used_bodies
            ),
            None,
        )
        if choice is None:
            choice = next(
                (item for item in options if item["brand"] not in used_brands), None
            )
        if choice is None:
            raise RuntimeError(f"Cannot select a distinct brand for {label} smoke case")
        used_brands.add(choice["brand"])
        used_models.add((choice["brand"], choice["model"]))
        used_bodies.add(choice["body"])
        preferred_year = 2022 if label == "common" else 2025
        year = (
            int(choice["year_to"])
            if label == "older"
            else max(choice["year_from"], min(choice["year_to"] or 2026, preferred_year))
        )
        price = 1_500_000 if label == "older" else 4_000_000 if label == "newer" else 3_000_000
        chosen.append((label, SearchRequest(
            brand=choice["brand"], model=choice["model"], year=year,
            body_type=choice["body"], price=price, debug=True,
        )))
    return chosen


async def run(delay: float, output: Path | None = None) -> list[dict]:
    rows = []
    for index, (label, query) in enumerate(select_cases()):
        started = time.perf_counter()
        result = await SearchService().search(query)
        operations = {
            source: {name: data.model_dump(mode="json") for name, data in values.items()}
            for source, values in result.source_operations.items()
        }
        row = {
            "case": label,
            "query": query.model_dump(mode="json"),
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "source_order": list(operations),
            "source_status": result.source_status,
            "operations": operations,
            "source_market": result.source_distribution,
            "direct": len(result.direct.listings),
            "expensive": len(result.expensive.listings),
            "cheaper": len(result.cheaper.listings),
            "pipeline": result.pipeline_diagnostics,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if index + 1 < 3:
            await asyncio.sleep(delay)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    asyncio.run(run(max(args.delay, 0), args.output))


if __name__ == "__main__":
    main()
