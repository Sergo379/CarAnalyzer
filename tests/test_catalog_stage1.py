import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from backend.models.car import SearchRequest
from backend.models.catalog import (
    EngineSpec,
    SourceReference,
    VehicleGeneration,
    VehicleModification,
)
from backend.models.listing import CarListing
from backend.services.marketplace_catalog import CatalogCache, CatalogEnrichmentService
from backend.services.search_service import SearchService
from backend.tools.catalog_audit import audit_catalog
from backend.tools.catalog_enrich import _eligible, enrich_all


def _catalog(tmp_path):
    cache = CatalogCache(path=tmp_path / "catalog.json")
    cache.save(
        {
            "version": 3,
            "brands": [
                {
                    "id": "make",
                    "name": "Make",
                    "source_refs": [],
                    "models": [
                        {
                            "id": f"make:{name.casefold()}",
                            "name": name,
                            "source_refs": [
                                {
                                    "source": "drom.ru",
                                    "url": f"https://www.drom.ru/catalog/make/{name.casefold()}/",
                                    "path": f"/catalog/make/{name.casefold()}/",
                                    "slug": name.casefold(),
                                }
                            ],
                            "generations": [],
                            "details_status": "not_checked",
                        }
                        for name in ("Alpha", "Beta")
                    ],
                }
            ],
        }
    )
    return cache


def _generation(name: str, body: str, year_to: int | None, modification_id: str):
    ref = SourceReference(
        source="drom.ru",
        url="https://www.drom.ru/catalog/make/alpha/g_2000_1/",
        path="/catalog/make/alpha/g_2000_1/",
        slug="g_2000_1",
    )
    return VehicleGeneration(
        id="drom:g_2000_1",
        name=name,
        year_from=2000,
        year_to=year_to,
        body_types=[body],
        source_refs=[ref],
        modifications=[
            VehicleModification(
                id=modification_id,
                name="2.0 AT",
                engine=EngineSpec(fuel_type="бензин", displacement_l=2.0, power_hp=150),
                source_refs=[ref],
            )
        ],
    )


def test_generation_merge_unions_bodies_deduplicates_modifications_and_keeps_open_end(tmp_path):
    cache = _catalog(tmp_path)
    cache.save_model_details("Make", "Alpha", [_generation("First", "sedan", None, "m1")])
    cache.save_model_details("Make", "Alpha", [_generation("First", "wagon", 2010, "m1")])
    generation = cache.model_entry("Make", "Alpha")["generations"][0]
    assert generation["year_to"] is None
    assert generation["body_types"] == ["sedan", "wagon"]
    assert len(generation["modifications"]) == 1
    assert len(cache.engine_options("Make", "Alpha")) == 1


def test_model_freshness_is_independent_of_requested_year_and_negative_status_cached(tmp_path):
    cache = _catalog(tmp_path)
    cache.save_model_details("Make", "Alpha", [_generation("First", "sedan", 2010, "m1")])
    assert cache.details_are_fresh_window("Make", "Alpha", 2022, 2022)
    assert cache.generations("Make", "Alpha", 2022) == []
    asyncio.run(cache.merge_model_observation("Make", "Beta", "source_has_no_generation_data"))
    assert cache.details_are_fresh_window("Make", "Beta", 2022, 2022)
    assert cache.model_entry("Make", "Beta")["generations"] == []


def test_source_with_no_generation_markup_gets_explicit_negative_status(tmp_path):
    cache = _catalog(tmp_path)
    service = CatalogEnrichmentService(cache)

    async def check():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text="<h1>Make Beta</h1>")
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await service.enrich_model("Make", "Beta", client)

    assert asyncio.run(check()) == "source_has_no_generation_data"
    assert cache.model_entry("Make", "Beta")["details_status"] == "source_has_no_generation_data"
    assert cache.details_are_fresh_window("Make", "Beta", 2000, 2000)


def test_legacy_timestamp_without_observation_status_is_not_fresh(tmp_path):
    cache = _catalog(tmp_path)
    entry = cache.model_entry("Make", "Alpha")
    entry.pop("details_status")
    entry["details_updated_at"] = datetime.now(UTC).isoformat()
    cache.save(cache.load())
    assert not cache.details_are_fresh_window("Make", "Alpha", 2020, 2020)


def test_outdated_parser_version_is_refetched_and_replaced_without_old_years(tmp_path):
    cache = _catalog(tmp_path)
    old = _generation("First", "sedan", None, "m1").model_copy(update={"year_from": 1999})
    cache.save_model_details("Make", "Alpha", [old])
    entry = cache.model_entry("Make", "Alpha")
    entry["details_parser_version"] = 1
    cache.save(cache.load())
    assert not cache.details_are_fresh_window("Make", "Alpha", 2026, 2026)
    assert _eligible(entry, retry_failed=False)
    stale_report = audit_catalog(cache)
    assert stale_report["summary"]["needs_reparse"] == 1
    assert stale_report["summary"]["pending"] == 2
    corrected = old.model_copy(update={"year_from": 2026})
    asyncio.run(
        cache.merge_model_observation(
            "Make", "Alpha", "complete", [corrected], replace_source="drom.ru"
        )
    )
    fixed = cache.model_entry("Make", "Alpha")
    assert fixed["generations"][0]["year_from"] == 2026
    assert fixed["details_parser_version"] == 3
    assert audit_catalog(cache)["summary"]["validated_complete"] == 1


def test_concurrent_model_observations_do_not_lose_updates(tmp_path):
    cache = _catalog(tmp_path)

    async def run():
        await asyncio.gather(
            cache.merge_model_observation(
                "Make", "Alpha", "complete", [_generation("First", "sedan", 2010, "m1")]
            ),
            cache.merge_model_observation("Make", "Beta", "source_has_no_generation_data"),
        )

    asyncio.run(run())
    reloaded = CatalogCache(path=cache.path)
    assert reloaded.model_entry("Make", "Alpha")["details_status"] == "complete"
    assert reloaded.model_entry("Make", "Beta")["details_status"] == "source_has_no_generation_data"


def test_atomic_save_retries_transient_windows_reader_collision(tmp_path, monkeypatch):
    import backend.services.marketplace_catalog as catalog_module

    cache = _catalog(tmp_path)
    original_replace = catalog_module.os.replace
    attempts = 0

    def transient_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("reader still has catalog open")
        return original_replace(source, destination)

    monkeypatch.setattr(catalog_module.os, "replace", transient_replace)
    cache.save_model_details("Make", "Alpha", [_generation("First", "sedan", 2010, "m1")])
    assert attempts == 3
    assert (
        CatalogCache(path=cache.path).model_entry("Make", "Alpha")["details_status"] == "complete"
    )


def test_new_generation_markup_merges_sections_without_fabricating_years():
    html = b"""
    <div><h3>Make Alpha</h3><span>1 generation</span>
      <div data-ga-stats-name="generations_outlet_item"><a href="/catalog/make/alpha/g_2000_1/">
      <span data-ftid="component_article_caption">2000 - present</span>
      <div>sedan</div></a></div>
      <div data-ga-stats-name="generations_outlet_item"><a href="/catalog/make/alpha/g_2000_2/">
      <span data-ftid="component_article_caption">2000 - present</span>
      <div>wagon</div></a></div>
    </div>
    """
    # This English fixture deliberately has no known body translation; the
    # integrity property is unknown years and merged source references.
    generations = CatalogEnrichmentService.parse_drom_year(
        html, "Make", "Alpha", 2025, "https://www.drom.ru/catalog/make/alpha/"
    )
    assert len(generations) == 1
    assert generations[0].year_from == 2000
    assert generations[0].year_to is None
    assert len(generations[0].source_refs) == 2
    without_period = html.replace(b"2000 - present", b"unknown")
    unknown = CatalogEnrichmentService.parse_drom_year(
        without_period, "Make", "Alpha", 2025, "https://www.drom.ru/catalog/make/alpha/"
    )
    assert unknown[0].year_from is None
    assert unknown[0].year_to is None


def test_generation_parser_accepts_nested_catalog_group_for_same_model():
    html = b"""
    <div><h3>Make Alpha, 2015 - present</h3>
      <div data-ga-stats-name="generations_outlet_item">
        <a href="/catalog/lcv/make/alpha/g_2015_1/">
          <span data-ftid="component_article_caption">2015 - present</span>
        </a>
      </div>
      <div data-ga-stats-name="generations_outlet_item">
        <a href="/catalog/lcv/make/beta/g_2015_2/">Other model</a>
      </div>
    </div>
    """
    generations = CatalogEnrichmentService.parse_drom_year(
        html, "Make", "Alpha", 2020, "https://www.drom.ru/catalog/make/alpha/"
    )
    assert len(generations) == 1
    assert generations[0].source_refs[0].path == "/catalog/lcv/make/alpha/g_2015_1/"


def test_generation_parser_scopes_cards_to_their_own_heading():
    html = b"""
    <div>
      <h3>Make Alpha, 2026 - present</h3><span>2 generation</span>
      <div><div data-ga-stats-name="generations_outlet_item">
        <a href="/catalog/make/alpha/g_2026_2/">
        <span data-ftid="component_article_caption">2026 - present</span></a></div></div>
      <h3>Make Alpha, 1999 - 2003</h3><span>1 generation</span>
      <div><div data-ga-stats-name="generations_outlet_item">
        <a href="/catalog/make/alpha/g_1999_1/">
        <span data-ftid="component_article_caption">1999 - 2003</span></a></div></div>
    </div>
    """
    items = CatalogEnrichmentService.parse_drom_year(
        html, "Make", "Alpha", 2020, "https://www.drom.ru/catalog/make/alpha/"
    )
    assert [(item.year_from, item.year_to) for item in items] == [(2026, None), (1999, 2003)]
    assert [len(item.source_refs) for item in items] == [1, 1]


def test_full_offline_audit_reports_explicit_empty_status_and_alias_collision(tmp_path):
    cache = _catalog(tmp_path)
    asyncio.run(cache.merge_model_observation("Make", "Alpha", "source_has_no_generation_data"))
    report = audit_catalog(cache)
    assert report["summary"]["models_audited"] == 2
    assert report["summary"]["source_has_no_generation_data"] == 1
    assert report["summary"]["pending"] == 1
    assert report["summary"]["generation_coverage_pct"] == 0
    assert report["records"][0]["details_status"] == "source_has_no_generation_data"
    payload = cache.load()
    payload["brands"][0]["models"][1]["aliases"] = ["Alpha"]
    cache.save(payload)
    assert any(error["type"] == "alias_collision" for error in audit_catalog(cache)["errors"])


def test_catalog_completeness_audit_detects_missing_source_models():
    class StaticCache:
        def load(self):
            return {
                "brands": [
                    {
                        "id": "make",
                        "name": "Make",
                        "models": [
                            {
                                "id": f"make:{name.casefold()}",
                                "name": name,
                                "source_refs": [
                                    {
                                        "source": "drom.ru",
                                        "url": f"https://www.drom.ru/catalog/make/{name.casefold()}/",
                                        "path": f"/catalog/make/{name.casefold()}/",
                                        "slug": name.casefold(),
                                    }
                                ],
                            }
                            for name in ("Alpha", "Beta")
                        ],
                    }
                ]
            }

    cache = StaticCache()
    without_index = audit_catalog(cache)
    assert without_index["summary"]["source_index_available"] is False

    report = audit_catalog(cache, {"drom.ru": {"make": 3}})
    completeness = report["per_brand"][0]["source_model_completeness"]["drom.ru"]
    assert completeness == {
        "source_model_count": 3,
        "stored_model_count": 2,
        "stored_source_ref_count": 2,
        "missing_model_count": 1,
        "canonical_alias_collapses": 0,
        "coverage_pct": 66.67,
    }
    assert report["summary"]["source_index_brands_with_gaps"] == 1
    assert report["summary"]["source_index_missing_mappings"] == 1
    assert any(error["type"] == "source_model_count_gap" for error in report["errors"])


def test_resumable_enrichment_and_retry_failed(tmp_path, monkeypatch):
    cache = _catalog(tmp_path)
    calls = []

    async def fake_enrich(self, brand, model, client, *, retry_failed=False):
        calls.append(model)
        await self.cache.merge_model_observation(
            brand, model, "complete", [_generation("First", "sedan", 2010, f"m-{model}")]
        )
        return "complete"

    monkeypatch.setattr(CatalogEnrichmentService, "enrich_model", fake_enrich)
    first = asyncio.run(enrich_all(cache, max_models=1))
    second = asyncio.run(enrich_all(cache, max_models=1))
    assert first["processed"] == second["processed"] == 1
    assert calls == ["Alpha", "Beta"]
    entry = cache.model_entry("Make", "Beta")
    entry["details_status"] = "parse_error"
    entry["details_retry_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    cache.save(cache.load())
    assert not _eligible(entry, retry_failed=False)
    assert _eligible(entry, retry_failed=True)


def test_generation_filter_accepts_unknown_but_rejects_known_mismatch(tmp_path):
    cache = _catalog(tmp_path)
    first = _generation("First", "sedan", 2010, "m1")
    second = first.model_copy(update={"id": "drom:g_2000_2", "name": "Second"})
    cache.save_model_details("Make", "Alpha", [first, second])
    service = SearchService(scrapers=[])
    service.marketplace_catalog = cache
    query = SearchRequest(
        brand="Make",
        model="Alpha",
        year=2005,
        body_type="any",
        price=1_000_000,
        generation_id=first.id,
    )
    listing = CarListing(
        source="drom.ru",
        external_id="1",
        brand="Make",
        model="Alpha",
        canonical_model_id="make:alpha",
        year=2005,
        body_type="sedan",
        price=1_000_000,
        url="https://auto.drom.ru/make/alpha/1.html",
        location="Москва",
        checked_at=datetime.now(UTC),
    )
    assert service._source_rejection_reason(listing, query) is None
    listing.canonical_generation_id = second.id
    assert service._source_rejection_reason(listing, query) == "generation"
    listing.canonical_generation_id = first.id
    assert service._source_rejection_reason(listing, query) is None
