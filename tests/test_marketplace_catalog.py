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
