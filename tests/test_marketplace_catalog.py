import asyncio
import json
from pathlib import Path

from backend.services.marketplace_catalog import (
    CatalogCache,
    CatalogEnrichmentService,
    CatalogSyncService,
    PublicHtmlCatalogSource,
    SourceBrand,
    SourceCatalog,
    SourceModel,
)


class FakeSource:
    def __init__(self, name: str, brands: dict[str, list[str]]) -> None:
        self.name = name
        self.brands = brands

    async def fetch(self) -> SourceCatalog:
        return SourceCatalog(
            self.name,
            [
                SourceBrand(
                    brand,
                    f"https://{self.name}/catalog/{brand.casefold()}/",
                    [
                        SourceModel(
                            model,
                            f"https://{self.name}/catalog/{brand.casefold()}/"
                            f"{model.casefold().replace(' ', '-')}/",
                        )
                        for model in models
                    ],
                )
                for brand, models in self.brands.items()
            ],
            detail="fixture",
        )


def test_public_html_catalog_parser_reads_only_brand_and_model_depths() -> None:
    source = PublicHtmlCatalogSource(
        "example", "https://example.test/catalog/cars/", ("catalog", "cars")
    )
    index = """
        <a href="/catalog/cars/bmw/">BMW</a>
        <a href="/catalog/cars/bmw/x5/">BMW X5</a>
        <a href="/catalog/cars/">All cars</a>
    """
    brand = """
        <a href="/catalog/cars/bmw/x5/">X5</a>
        <a href="/catalog/cars/bmw/5_series/">5 Series</a>
        <a href="/catalog/cars/bmw/x5/g05/">G05</a>
    """
    assert source._brand_links(index) == {"BMW": "https://example.test/catalog/cars/bmw/"}
    assert [
        item.name for item in source._models(brand, "https://example.test/catalog/cars/bmw/", "BMW")
    ] == ["X5", "5 Series"]


def test_drom_model_parser_excludes_navigation_and_deduplicates_cards() -> None:
    source = PublicHtmlCatalogSource(
        "drom.ru",
        "https://www.drom.ru/catalog/",
        ("catalog",),
        excluded_model_slugs=frozenset({"engine", "frame"}),
    )
    html = """
      <a href="/catalog/bmw/x5/">X5</a>
      <a href="/catalog/bmw/engine/">Двигатели BMW</a>
      <a href="/catalog/bmw/frame/">Кузова BMW</a>
      <a href="/catalog/bmw/x5/">BMW X5</a>
    """
    models = source._models(html, "https://www.drom.ru/catalog/bmw/", "BMW")
    assert [(item.name, item.url) for item in models] == [
        ("X5", "https://www.drom.ru/catalog/bmw/x5/")
    ]


def test_drom_generation_engine_and_power_parsing() -> None:
    html = """
      <h3><a href="/catalog/bmw/x5/g_2018_8395/">
        4 поколение, 06.2018 - 03.2022, Джип/SUV 5 дв.
      </a></h3>
      <table class="complectation-table">
        <tr><th>Комплектация</th><th>Период</th><th>Цена</th>
          <th>Марка двигателя</th><th>Кузов</th></tr>
        <tr><th colspan="5">3.0 л, 249 л.с., Дизель, АКПП, Полный (4WD)</th></tr>
        <tr><td><a href="/catalog/bmw/x5/207354/">xDrive 30d AT Base</a></td>
          <td>2018-2022</td><td></td><td>B57D30</td><td>G05</td></tr>
      </table>
    """.encode()
    generations = CatalogEnrichmentService.parse_drom_year(
        html, "BMW", "X5", 2018, "https://www.drom.ru/catalog/bmw/x5/2018/"
    )
    assert generations[0].name == "G05 · 4 поколение"
    modification = generations[0].modifications[0]
    assert modification.engine.displacement_l == 3.0
    assert modification.engine.power_hp == 249
    assert modification.engine.fuel_type == "дизель"
    assert modification.engine.engine_code == "B57D30"
    assert modification.source_refs[0].path == "/catalog/bmw/x5/207354/"


def _cache_path(name: str) -> Path:
    path = Path("data") / name
    path.unlink(missing_ok=True)
    return path


def test_sync_merges_sources_normalizes_aliases_and_persists_cache() -> None:
    cache_path = _cache_path("merge.json")
    service = CatalogSyncService(
        sources=[
            FakeSource("auto.ru", {"Mercedes Benz": ["E Series"], "BMW": ["5 серии"]}),
            FakeSource("drom.ru", {"Mercedes-Benz": ["E-Series"], "BMW": ["5-Series", "X5"]}),
        ],
        cache=CatalogCache(cache_path),
    )

    status = asyncio.run(service.sync())

    assert status["brand_count"] == 2
    assert status["model_count"] == 3
    cache = CatalogCache(cache_path)
    assert cache.brands() == ["BMW", "Mercedes-Benz"]
    assert cache.models("Mercedes") == ["E Series"]
    assert cache.models("BMW") == ["5 серии", "X5"]
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["catalog_updated_at"]
    assert payload["sources"]["auto.ru"]["status"] == "ok"
    cache_path.unlink()


def test_failed_sync_preserves_existing_cache() -> None:
    cache_path = _cache_path("failure.json")
    cache_path.write_text('{"version": 1, "brands": []}', encoding="utf-8")

    class BrokenSource:
        name = "broken"

        async def fetch(self) -> SourceCatalog:
            raise RuntimeError("offline")

    try:
        asyncio.run(CatalogSyncService([BrokenSource()], CatalogCache(cache_path)).sync())
    except RuntimeError as error:
        assert "local cache was preserved" in str(error)
    else:
        raise AssertionError("sync should fail when every source is unavailable")
    assert json.loads(cache_path.read_text(encoding="utf-8"))["version"] == 1
    cache_path.unlink()


def test_sync_does_not_merge_one_edit_model_names_without_explicit_alias() -> None:
    cache_path = _cache_path("near-alias.json")
    service = CatalogSyncService(
        sources=[
            FakeSource("first.test", {"Example": ["Roadstar"]}),
            FakeSource("second.test", {"Example": ["Roadster"]}),
        ],
        cache=CatalogCache(cache_path),
    )
    asyncio.run(service.sync())
    entries = CatalogCache(cache_path).model_entries("Example")
    assert [entry["name"] for entry in entries] == ["Roadstar", "Roadster"]
    cache_path.unlink()


def test_runtime_cache_starts_from_seed_and_never_writes_seed() -> None:
    seed_path = _cache_path("seed.json")
    runtime_path = _cache_path("runtime.json")
    seed_payload = {"version": 3, "sources": {}, "brands": []}
    seed_path.write_text(json.dumps(seed_payload), encoding="utf-8")
    cache = CatalogCache(seed_path=seed_path, runtime_path=runtime_path)

    assert cache.load() == seed_payload
    cache.save({**seed_payload, "catalog_updated_at": "runtime"})

    assert json.loads(seed_path.read_text(encoding="utf-8")) == seed_payload
    assert json.loads(runtime_path.read_text(encoding="utf-8"))["catalog_updated_at"] == "runtime"
    seed_path.unlink()
    runtime_path.unlink()


def test_sync_preserves_unavailable_source_refs_and_enrichment() -> None:
    cache_path = _cache_path("non-destructive-merge.json")
    cache_path.write_text(
        json.dumps(
            {
                "version": 3,
                "sources": {"offline.test": {"status": "ok"}},
                "brands": [
                    {
                        "id": "example",
                        "name": "Example",
                        "aliases": [],
                        "source_refs": [],
                        "models": [
                            {
                                "id": "example:one",
                                "name": "One",
                                "aliases": [],
                                "source_refs": [
                                    {
                                        "source": "offline.test",
                                        "url": "https://offline.test/one/",
                                        "path": "/one/",
                                        "slug": "one",
                                    }
                                ],
                                "generations": [{"id": "known", "name": "Known"}],
                                "details_updated_at": "2026-01-01T00:00:00+00:00",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    asyncio.run(
        CatalogSyncService(
            [FakeSource("online.test", {"Example": ["One", "Two"]})],
            CatalogCache(cache_path),
        ).sync()
    )
    cache = CatalogCache(cache_path)
    one = cache.model_entry("Example", "One")
    assert one is not None
    assert {ref["source"] for ref in one["source_refs"]} == {
        "offline.test",
        "online.test",
    }
    assert one["generations"] == [{"id": "known", "name": "Known"}]
    assert cache.model_entry("Example", "Two") is not None
    cache_path.unlink()


def test_identity_resolution_is_exact_and_collision_safe() -> None:
    cache = CatalogCache()
    assert cache.resolve_identity("Audi", "A5").canonical_model_id == "audi:a5"
    assert cache.resolve_identity("Audi", "A5L").canonical_model_id == "audi:a5l"
    assert cache.resolve_identity("BMW", "X5").canonical_model_id == "bmw:x5"
    assert cache.resolve_identity("BMW", "iX5").canonical_model_id == "bmw:ix5"


def test_xray_script_variants_resolve_without_collapsing_cross() -> None:
    cache = CatalogCache()
    expected = cache.resolve_identity("Lada", "Х-рей").canonical_model_id
    assert expected == "lada:хрей"
    for value in ("X-ray", "XRAY", "xray", "X-рей"):
        assert cache.resolve_identity("Lada", value).canonical_model_id == expected
    assert cache.resolve_identity("Lada", "XRAY Cross").canonical_model_id == "lada:хрейкросс"


def test_engine_options_deduplicate_trims_by_engine_spec() -> None:
    cache = CatalogCache()
    options = cache.engine_options("Audi", "A5", year=2020)
    assert options
    assert len(options) < len(cache.modifications("Audi", "A5", year=2020))
    assert all(str(option["id"]).startswith("engine:") for option in options)
