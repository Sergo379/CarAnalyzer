"""Deterministic, data-driven Drom target and discovery smoke.

The selector reads the immutable catalog snapshot and chooses models by data
properties before making any network request. ``--select-only`` is offline.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from time import perf_counter

from backend.config import Settings
from backend.models.car import BodyType, MarketDiscoveryRequest, SearchRequest
from backend.scrapers.drom import DromScraper
from backend.services.marketplace_catalog import CATALOG_SEED_PATH, CatalogCache


def _rank(item: dict) -> str:
    identity = f"catalog-stage1-live-v1|{item['brand_id']}|{item['model_id']}"
    return hashlib.sha256(identity.encode()).hexdigest()


def select_sample(cache: CatalogCache, count: int = 8) -> list[dict]:
    rows = []
    for brand in cache.load().get("brands", []):
        models = brand.get("models", [])
        for model in models:
            if not any(ref.get("source") == "drom.ru" for ref in model.get("source_refs", [])):
                continue
            generations = model.get("generations", [])
            bodies = {
                body for generation in generations for body in generation.get("body_types", [])
            }
            starts = [g["year_from"] for g in generations if g.get("year_from") is not None]
            ends = [g["year_to"] for g in generations if g.get("year_to") is not None]
            similar = any(
                other is not model
                and (
                    str(other["name"]).casefold().startswith(str(model["name"]).casefold())
                    or str(model["name"]).casefold().startswith(str(other["name"]).casefold())
                )
                for other in models
            )
            rows.append(
                {
                    "brand_id": brand["id"],
                    "model_id": model["id"],
                    "brand": brand["name"],
                    "model": model["name"],
                    "generations": len(generations),
                    "body_types": sorted(bodies),
                    "starts": starts,
                    "ends": ends,
                    "similar_name": similar,
                }
            )
    predicates = {
        "with_generations": lambda row: row["generations"] > 0,
        "previously_unenriched": lambda row: row["generations"] == 0,
        "modern": lambda row: bool(row["starts"] and max(row["starts"]) >= 2015),
        "historic": lambda row: bool(row["ends"] and max(row["ends"]) < 2000),
        "single_body": lambda row: len(row["body_types"]) == 1,
        "multiple_bodies": lambda row: len(row["body_types"]) > 1,
        "multiword_name": lambda row: " " in row["brand"] or " " in row["model"],
        "similar_names": lambda row: row["similar_name"],
    }
    selected: dict[str, dict] = {}
    for feature, predicate in predicates.items():
        candidates = sorted(
            (row for row in rows if predicate(row) and row["model_id"] not in selected),
            key=_rank,
        )
        if candidates and len(selected) < count:
            existing_brands = {item["brand_id"] for item in selected.values()}
            choice = next(
                (row for row in candidates if row["brand_id"] not in existing_brands),
                candidates[0],
            ).copy()
            choice["selected_for"] = feature
            selected[choice["model_id"]] = choice
    for row in sorted(rows, key=_rank):
        if len(selected) >= count:
            break
        if row["model_id"] not in selected:
            choice = row.copy()
            choice["selected_for"] = "hash_fill"
            selected[choice["model_id"]] = choice
    return list(selected.values())


async def live_sample(sample: list[dict], pages: int = 2) -> list[dict]:
    results = []
    current_year = datetime.now(UTC).year
    for index, item in enumerate(sample):
        if index:
            await asyncio.sleep(2)
        start = max(1900, min(item["starts"])) if item["starts"] else 1900
        end = min(current_year, max(item["ends"])) if item["ends"] else current_year
        if item["generations"] > len(item["ends"]):
            end = current_year
        if start > end:
            start, end = 2000, current_year
        query = SearchRequest(
            brand=item["brand"],
            model=item["model"],
            year_mode="range",
            year_from=start,
            year_to=end,
            body_type="any",
            region="any",
            price_mode="range",
            price_from=1,
            price_to=50_000_000,
        )
        discovery = MarketDiscoveryRequest(
            source_vehicle=query,
            price_from=500_000,
            price_to=5_000_000,
            body_types=frozenset(BodyType),
        )
        scraper = DromScraper(settings=Settings(_env_file=None, scraper_max_pages=pages))

        async def measured(kind: str, operation, current_scraper: DromScraper = scraper):
            operation_started = perf_counter()
            try:
                return await operation
            finally:
                diagnostic = current_scraper.diagnostics.get(kind)
                if diagnostic is not None:
                    diagnostic.elapsed_seconds = round(perf_counter() - operation_started, 2)

        started = perf_counter()
        target, competitors = await asyncio.gather(
            measured("target", scraper.search(query)),
            measured("competitors", scraper.discover(discovery)),
            return_exceptions=True,
        )
        results.append(
            {
                "brand": item["brand"],
                "model": item["model"],
                "selected_for": item["selected_for"],
                "target_count": len(target) if isinstance(target, list) else 0,
                "target_state": type(target).__name__
                if isinstance(target, Exception)
                else (
                    "partial"
                    if scraper.diagnostics.get("target") and scraper.diagnostics["target"].degraded
                    else "ok"
                ),
                "competitor_count": len(competitors) if isinstance(competitors, list) else 0,
                "competitor_state": type(competitors).__name__
                if isinstance(competitors, Exception)
                else (
                    "partial"
                    if scraper.diagnostics.get("competitors")
                    and scraper.diagnostics["competitors"].degraded
                    else "ok"
                ),
                "elapsed_seconds": round(perf_counter() - started, 2),
                "diagnostics": {
                    key: value.model_dump(mode="json") for key, value in scraper.diagnostics.items()
                },
            }
        )
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select-only", action="store_true")
    parser.add_argument("--seed", action="store_true", help="Select from immutable release seed")
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--pages", type=int, default=2)
    args = parser.parse_args()
    sample = select_sample(
        CatalogCache(path=CATALOG_SEED_PATH) if args.seed else CatalogCache(), args.count
    )
    print(json.dumps({"selection": sample}, ensure_ascii=False, indent=2), flush=True)
    if not args.select_only:
        asyncio.run(live_sample(sample, args.pages))


if __name__ == "__main__":
    main()
