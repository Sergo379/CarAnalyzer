"""Synthetic, network-free CarsBase inventory and lazy Drom mapping regressions."""

import asyncio

import httpx
import pytest

from backend.api import search as catalog_api
from backend.models.car import BodyType, SearchRequest
from backend.models.catalog import SourceReference
from backend.scrapers.base import ScraperParseError
from backend.scrapers.drom import DromScraper
from backend.services.carsbase_catalog import (
    CarsBaseClient,
    CarsBaseError,
    CarsBaseSyncService,
    parse_full,
    parse_status,
    reconcile,
)
from backend.services.drom_model_mapping import DromModelResolver
from backend.services.marketplace_catalog import CatalogCache, CatalogEnrichmentService
from backend.services.segment_classifier import SegmentClassifier, classify_metadata
from backend.tools.catalog_audit import audit_catalog


def upstream(*, name="Alpha", model_id="M1", model_class="J"):
    return {
        "data": [
            {
                "id": "B1",
                "name": "Example",
                "cyrillic_name": "Пример",
                "numeric_id": 1,
                "country": "Exampleland",
                "year_from": 1990,
                "year_to": 2026,
                "popular": True,
                "updated_at": "2026-01-01 00:00:00",
                "models": [
                    {
                        "id": model_id,
                        "mark_id": "B1",
                        "name": name,
                        "cyrillic_name": "Альфа",
                        "year_from": 2000,
                        "year_to": 2026,
                        "class": model_class,
                        "updated_at": "2026-01-01 00:00:00",
                    }
                ],
            }
        ],
        "meta": {"cached": True},
    }


def status(version="2026-01-01 00:00:00", models=1):
    return {"last_update": version, "counts": {"marks": 1, "models": models}, "meta": {}}


def legacy_catalog():
    return {
        "version": 3,
        "sources": {"drom.ru": {"status": "ok"}},
        "brands": [
            {
                "id": "example",
                "name": "Example",
                "aliases": [],
                "source_refs": [],
                "models": [
                    {
                        "id": "example:alpha",
                        "name": "Alpha",
                        "aliases": [],
                        "source_refs": [
                            {
                                "source": "auto.ru",
                                "url": "https://auto.test/alpha/",
                                "path": "/alpha/",
                                "slug": "alpha",
                            }
                        ],
                        "generations": [
                            {"id": "old-generation", "name": "Old", "modifications": []}
                        ],
                        "details_status": "complete",
                        "classification": {"segment_code": "J-C"},
                    },
                    {
                        "id": "example:legacy",
                        "name": "Legacy",
                        "aliases": [],
                        "source_refs": [],
                        "generations": [],
                    },
                ],
            }
        ],
    }


def client_for(full=None, *, version="2026-01-01 00:00:00", status_code=200, full_code=200):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/status":
            return httpx.Response(status_code, json=status(version))
        if request.url.path == "/full":
            return httpx.Response(full_code, json=full if full is not None else upstream())
        raise AssertionError("Unexpected request")

    return CarsBaseClient(transport=httpx.MockTransport(handler)), calls


def test_live_schema_shape_is_parsed_without_inventing_fields():
    current = parse_status(status())
    snapshot = parse_full(upstream(), current)
    assert current["model_count"] == snapshot.model_count == 1
    assert snapshot.brands[0].metadata["country"] == "Exampleland"
    assert snapshot.brands[0].models[0].metadata["class"] == "J"
    assert "market_position" not in snapshot.brands[0].models[0].metadata
    assert (
        SourceReference(source="drom.ru", url="https://drom.test/", path="/", slug="x").external_id
        is None
    )


def test_carsbase_client_bounds_streamed_status_payload():
    client = CarsBaseClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b" " * 1_000_001)
        )
    )
    with pytest.raises(CarsBaseError, match="oversized_response"):
        asyncio.run(client.status())


@pytest.mark.parametrize("mutation", ["empty", "duplicate_model", "wrong_brand", "short_count"])
def test_invalid_or_truncated_full_is_rejected(mutation):
    payload = upstream()
    if mutation == "empty":
        payload["data"] = []
    elif mutation == "duplicate_model":
        payload["data"][0]["models"].append(payload["data"][0]["models"][0].copy())
    elif mutation == "wrong_brand":
        payload["data"][0]["models"][0]["mark_id"] = "OTHER"
    with pytest.raises(CarsBaseError):
        parse_full(payload, parse_status(status(models=2 if mutation == "short_count" else 1)))


def test_reconcile_is_additive_id_stable_and_tracks_legacy_and_coverage():
    original = legacy_catalog()
    merged, report = reconcile(original, parse_full(upstream()))
    brand = merged["brands"][0]
    model = brand["models"][0]
    assert report["exact_matched_models"] == 1
    assert report["models_added"] == 0
    assert report["models_only_in_current"] == 1
    assert model["id"] == "example:alpha" and model["generations"][0]["id"] == "old-generation"
    assert model["source_refs"][0]["source"] == "auto.ru"
    assert model["source_refs"][1]["external_id"] == "M1"
    assert brand["source_refs"][0]["external_id"] == "B1"
    assert brand["carsbase_metadata"]["country"] == "Exampleland"
    assert model["carsbase_metadata"]["class"] == "J"
    assert original["brands"][0]["models"][0]["source_refs"] == model["source_refs"][:1]
    renamed, again = reconcile(merged, parse_full(upstream(name="Renamed Alpha")))
    assert again["models_added"] == 0
    assert renamed["brands"][0]["models"][0]["id"] == model["id"]
    assert "Renamed Alpha" in renamed["brands"][0]["models"][0]["aliases"]
    assert renamed["brands"][0]["models"][0]["generations"] == model["generations"]


def test_carsbase_only_model_and_alias_ambiguity_are_not_merged():
    payload = legacy_catalog()
    payload["brands"][0]["models"][0]["aliases"] = ["Shared"]
    payload["brands"][0]["models"][1]["aliases"] = ["Shared"]
    added, report = reconcile(payload, parse_full(upstream(name="Shared", model_id="M2")))
    assert report["models_added"] == 1
    assert report["models_only_in_carsbase"] == 1
    assert report["models_only_in_current"] == 2
    assert len(report["ambiguous_mappings"]) == 1
    assert report["alias_collisions"]
    assert len(added["brands"][0]["models"]) == 3
    assert all(
        m["id"] in {"example:alpha", "example:legacy"} for m in payload["brands"][0]["models"]
    )


def test_distinct_upstream_ids_with_same_exact_name_remain_separate_and_reported():
    data = upstream()
    data["data"][0]["models"].append({**data["data"][0]["models"][0], "id": "M2"})
    merged, report = reconcile(legacy_catalog(), parse_full(data))
    models = merged["brands"][0]["models"]
    assert report["exact_matched_models"] == 1
    assert report["models_added"] == 1
    assert report["ambiguous_mappings"]
    assert len({m["id"] for m in models}) == len(models)
    assert (
        len(
            {
                ref["external_id"]
                for m in models
                for ref in m["source_refs"]
                if ref["source"] == "cars-base.ru"
            }
        )
        == 2
    )


def test_existing_external_collision_and_year_class_conflicts_are_reported():
    payload = legacy_catalog()
    first = payload["brands"][0]["models"][0]
    first["carsbase_metadata"] = {"year_from": 1999, "class": "C"}
    second = payload["brands"][0]["models"][1]
    for model in (first, second):
        model["source_refs"].append(
            {
                "source": "cars-base.ru",
                "url": "https://api.cars-base.ru/full",
                "path": "/full",
                "slug": "",
                "external_id": "M1",
            }
        )
    _, collided = reconcile(payload, parse_full(upstream()))
    assert collided["external_id_collisions"]
    second["source_refs"] = []
    _, conflicted = reconcile(payload, parse_full(upstream(model_class="C")))
    assert conflicted["year_range_conflicts"]
    assert conflicted["class_segment_conflicts"]


def test_existing_generation_year_outside_carsbase_range_is_audit_only():
    payload = legacy_catalog()
    generation = payload["brands"][0]["models"][0]["generations"][0]
    generation["year_from"] = 1998
    merged, report = reconcile(payload, parse_full(upstream()))
    assert {"model_id": "example:alpha", "generation_id": "old-generation"} in report[
        "year_range_conflicts"
    ]
    assert merged["brands"][0]["models"][0]["generations"][0]["year_from"] == 1998


def test_status_unchanged_skips_full_and_dry_run_does_not_write(tmp_path):
    cache = CatalogCache(path=tmp_path / "catalog.json")
    payload = legacy_catalog()
    payload["sources"]["cars-base.ru"] = {
        "last_update": status()["last_update"],
        "last_successful_sync": "then",
    }
    cache.save(payload)
    before = cache.path.read_bytes()
    client, calls = client_for()
    unchanged = asyncio.run(CarsBaseSyncService(cache, client).sync())
    assert unchanged["status"] == "unchanged" and calls == ["/status"]
    audit = asyncio.run(CarsBaseSyncService(cache, client).sync(dry_run=True))
    assert audit["status"] == "dry_run" and calls[-2:] == ["/status", "/full"]
    assert cache.path.read_bytes() == before


@pytest.mark.parametrize(
    "case", ["status_unavailable", "status_invalid", "full_error", "full_429", "malformed", "empty"]
)
def test_failed_refresh_keeps_last_good_catalog(tmp_path, case):
    cache = CatalogCache(path=tmp_path / "catalog.json")
    cache.save(legacy_catalog())
    before = cache.path.read_bytes()

    def handler(request):
        if request.url.path == "/status":
            if case == "status_unavailable":
                return httpx.Response(503)
            if case == "status_invalid":
                return httpx.Response(200, json={"last_update": "bad"})
            return httpx.Response(200, json=status())
        if case == "full_error":
            return httpx.Response(500)
        if case == "full_429":
            return httpx.Response(429, headers={"Retry-After": "3"})
        if case == "malformed":
            return httpx.Response(200, text="{")
        return httpx.Response(200, json={"data": []})

    client = CarsBaseClient(transport=httpx.MockTransport(handler))
    outcome = asyncio.run(CarsBaseSyncService(cache, client).sync())
    assert outcome["status"] in {"error", "rate_limited"}
    assert outcome["last_good_preserved"] is True
    assert cache.path.read_bytes() == before
    if case == "full_429":
        assert outcome["retry_after_seconds"] == 3


def test_successful_sync_and_force_only_write_runtime_and_atomic_failure_preserves_file(
    tmp_path, monkeypatch
):
    cache = CatalogCache(path=tmp_path / "catalog.json")
    cache.save(legacy_catalog())
    client, calls = client_for()
    result = asyncio.run(CarsBaseSyncService(cache, client).sync())
    assert result["status"] == "synced"
    assert cache.status()["carsbase"]["model_count"] == 1
    assert cache.model_entry("Example", "Alpha")["generations"][0]["id"] == "old-generation"
    assert audit_catalog(cache)["summary"]["total_models"] == 2
    assert asyncio.run(CarsBaseSyncService(cache, client).sync(force=True))["status"] == "synced"
    assert calls.count("/full") == 2
    before = cache.path.read_bytes()

    def broken_save(self, payload):
        raise OSError("synthetic atomic save failure")

    monkeypatch.setattr(CatalogCache, "save", broken_save)
    with pytest.raises(OSError, match="synthetic atomic save failure"):
        asyncio.run(CarsBaseSyncService(cache, client).sync(force=True))
    assert cache.path.read_bytes() == before


def test_offline_audit_counts_carsbase_external_ids_not_shared_full_url(tmp_path):
    cache = CatalogCache(path=tmp_path / "catalog.json")
    data = upstream()
    data["data"][0]["models"].append({**data["data"][0]["models"][0], "id": "M2", "name": "Gamma"})
    cache.save(reconcile(legacy_catalog(), parse_full(data))[0])
    report = audit_catalog(cache, {"cars-base.ru": {"example": 2}})
    completeness = report["per_brand"][0]["source_model_completeness"]["cars-base.ru"]
    assert completeness["stored_source_ref_count"] == 2
    assert completeness["missing_model_count"] == 0


def test_catalog_brand_model_and_status_endpoints_stay_local(tmp_path, monkeypatch):
    cache = CatalogCache(path=tmp_path / "catalog.json")
    cache.save(reconcile(legacy_catalog(), parse_full(upstream()))[0])
    monkeypatch.setattr(catalog_api, "get_catalog_cache", lambda: cache)
    assert asyncio.run(catalog_api.catalog_brands()) == ["Example"]
    assert asyncio.run(catalog_api.catalog_models("Example")) == ["Alpha", "Legacy"]
    assert asyncio.run(catalog_api.catalog_status())["model_count"] == 2


def test_segment_classifier_reads_persisted_carsbase_class_without_market_position(tmp_path):
    cache = CatalogCache(path=tmp_path / "catalog.json")
    payload, _ = reconcile(legacy_catalog(), parse_full(upstream(model_class="C")))
    payload["brands"][0]["models"][0].pop("classification")
    cache.save(payload)
    classifier = SegmentClassifier(cache, database_path=tmp_path / "segments.db")
    result = asyncio.run(classifier.classify("Example", "Alpha", None, None))
    assert result.segment_code == "C"
    assert result.segment_family == "passenger"
    assert result.market_position == "unknown"

    payload["brands"][0]["models"][0]["carsbase_metadata"]["class"] = "J"
    cache.save(payload)
    fresh = SegmentClassifier(cache, database_path=tmp_path / "other-segments.db")
    broad = asyncio.run(fresh.classify("Example", "Alpha", None, None))
    assert broad.segment_code == "UNKNOWN" and broad.segment_family == "suv"


def test_carsbase_class_supplies_size_but_j_only_supplies_family():
    assert classify_metadata({"carsbase_class": "C"}, None).segment_code == "C"
    assert classify_metadata({"carsbase_class": "M"}, None).segment_family == "mpv"
    assert classify_metadata({"carsbase_class": "S"}, BodyType.COUPE).segment_code == "S"
    broad = classify_metadata({"carsbase_class": "J"}, None)
    assert broad.segment_family == "suv" and broad.segment_code == "UNKNOWN"
    assert broad.market_position == "unknown"
    assert classify_metadata(
        {"carsbase_class": "J", "dimensions": {"length_mm": 4650, "wheelbase_mm": 2750}}, None
    ).segment_code.startswith("J-")


def test_drom_mapping_is_lazy_exact_persistent_and_reused_by_enrichment(tmp_path):
    cache = CatalogCache(path=tmp_path / "catalog.json")
    cache.save(reconcile(legacy_catalog(), parse_full(upstream()))[0])
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/catalog/":
            return httpx.Response(200, text='<a href="/catalog/example/">Example</a>')
        if request.url.path == "/catalog/example/":
            return httpx.Response(200, text='<a href="/catalog/example/alpha/">Alpha</a>')
        if request.url.path == "/catalog/example/alpha/":
            return httpx.Response(200, text="<h1>Example Alpha</h1>")
        raise AssertionError(request.url.path)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = DromModelResolver(cache)
            assert await resolver.resolve("Example", "Alpha", client) == "mapped"
            assert await resolver.resolve("Example", "Alpha", client) == "mapped"
            assert (
                await CatalogEnrichmentService(cache).enrich_model(
                    "Example", "Alpha", client, retry_failed=True
                )
                == "source_has_no_generation_data"
            )

    asyncio.run(run())
    assert calls == ["/catalog/", "/catalog/example/", "/catalog/example/alpha/"]
    assert cache.source_model_ref("drom.ru", "Example", "Alpha").path == "/catalog/example/alpha/"
    assert cache.source_model_ref("cars-base.ru", "Example", "Alpha").external_id == "M1"
    assert cache.model_entry("Example", "Alpha")["generations"][0]["id"] == "old-generation"


@pytest.mark.parametrize(
    "case,expected",
    [("blocked", "source_unavailable"), ("limited", "rate_limited"), ("ambiguous", "ambiguous")],
)
def test_failed_drom_mapping_does_not_persist_fake_ref(tmp_path, case, expected):
    cache = CatalogCache(path=tmp_path / "catalog.json")
    cache.save(legacy_catalog())

    def handler(request):
        if request.url.path == "/catalog/":
            code = 403 if case == "blocked" else 429 if case == "limited" else 200
            return httpx.Response(code, text='<a href="/catalog/example/">Example</a>')
        return httpx.Response(
            200,
            text=(
                '<a href="/catalog/example/alpha/">Alpha</a>'
                '<a href="/catalog/example/alpha2/">Alpha</a>'
            ),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DromModelResolver(cache).resolve("Example", "Alpha", client)

    assert asyncio.run(run()) == expected
    assert cache.source_model_ref("drom.ru", "Example", "Alpha") is None


@pytest.mark.parametrize("blocked", [False, True])
def test_drom_target_search_uses_same_lazy_resolver_and_preserves_failure_isolation(
    tmp_path, monkeypatch, blocked
):
    cache = CatalogCache(path=tmp_path / "catalog.json")
    cache.save(reconcile(legacy_catalog(), parse_full(upstream()))[0])
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/catalog/":
            return httpx.Response(
                403 if blocked else 200,
                text='<a href="/catalog/example/">Example</a>',
            )
        if request.url.path == "/catalog/example/":
            return httpx.Response(200, text='<a href="/catalog/example/alpha/">Alpha</a>')
        if request.url.host == "auto.drom.ru":
            return httpx.Response(200, text="<html><body></body></html>")
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)
    original = DromModelResolver.resolve

    async def mapped_through_fixture(self, brand, model, client=None):
        async with httpx.AsyncClient(transport=transport) as fixture_client:
            return await original(self, brand, model, fixture_client)

    monkeypatch.setattr(DromModelResolver, "resolve", mapped_through_fixture)
    scraper = DromScraper(catalog=cache, transport=transport)
    query = SearchRequest(brand="Example", model="Alpha", year=2022, price=1_000_000)
    if blocked:
        with pytest.raises(ScraperParseError, match="mapping unavailable"):
            asyncio.run(scraper.search(query))
        assert cache.source_model_ref("drom.ru", "Example", "Alpha") is None
        assert not any("used" in path for path in calls)
    else:
        assert asyncio.run(scraper.search(query)) == []
        assert cache.source_model_ref("drom.ru", "Example", "Alpha") is not None
        assert asyncio.run(scraper.search(query)) == []
        assert calls.count("/catalog/") == 1
        assert sum("used" in path for path in calls) == 2
