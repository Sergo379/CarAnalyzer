"""Synthetic-only regression coverage for lazy canonical segment classification."""

import asyncio
import json
from datetime import UTC, datetime

import httpx

from backend.models.car import BodyType, Car, SearchRequest
from backend.models.listing import CarListing
from backend.scrapers.base import BaseScraper
from backend.services.competitor_engine import CompetitorEngine
from backend.services.marketplace_catalog import CatalogCache
from backend.services.search_service import SearchService
from backend.services.segment_classifier import (
    SegmentClassification,
    SegmentClassifier,
    classify_metadata,
    dimensions_from_catalog_html,
)


def catalog(
    tmp_path, *, model_classification=None, generation_classification=None, source_refs=None
):
    path = tmp_path / "synthetic-catalog.json"
    path.write_text(
        json.dumps(
            {
                "brands": [
                    {
                        "id": "brand-1",
                        "name": "Example Brand",
                        "models": [
                            {
                                "id": "model-1",
                                "name": "Example Model",
                                "classification": model_classification or {},
                                "generations": [
                                    {
                                        "id": "generation-1",
                                        "name": "Generation 1",
                                        "classification": generation_classification or {},
                                        "body_types": ["crossover"],
                                        "source_refs": source_refs or [],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return CatalogCache(path=path)


def test_source_metadata_wins_and_generation_overrides_model(tmp_path):
    classifier = SegmentClassifier(
        catalog(
            tmp_path,
            model_classification={"segment_code": "J-C", "category": "suv"},
            generation_classification={"segment_code": "J-D", "category": "suv"},
        ),
        tmp_path / "segments.db",
    )
    model = asyncio.run(
        classifier.classify("Example Brand", "Example Model", None, BodyType.CROSSOVER)
    )
    generation = asyncio.run(
        classifier.classify("Example Brand", "Example Model", "generation-1", BodyType.CROSSOVER)
    )
    assert (model.segment_code, generation.segment_code) == ("J-C", "J-D")
    assert generation.segment_method == "source"


def test_dimensions_distinguish_suv_subclasses_and_persist(tmp_path):
    cache = catalog(
        tmp_path,
        generation_classification={
            "category": "suv",
            "dimensions": {"length_mm": 4380, "wheelbase_mm": 2600},
        },
    )
    path = tmp_path / "segments.db"
    first = asyncio.run(
        SegmentClassifier(cache, path).classify(
            "Example Brand", "Example Model", "generation-1", BodyType.CROSSOVER
        )
    )
    second = asyncio.run(
        SegmentClassifier(cache, path).classify(
            "Example Brand", "Example Model", "generation-1", BodyType.CROSSOVER
        )
    )
    assert first.segment_code == "J-B"  # two dimensional signals, compact subclass
    assert first.segment_method == "dimensions"
    assert second.cache_hit and second.segment_code == first.segment_code
    assert (
        classify_metadata(
            {"category": "suv", "dimensions": {"length_mm": 5300, "wheelbase_mm": 3150}},
            BodyType.CROSSOVER,
        ).segment_code
        == "J-F"
    )


def test_displacement_alone_is_not_a_size_signal():
    result = classify_metadata(
        {"engine_displacement": 5.0, "category": "passenger"}, BodyType.SEDAN
    )
    assert result.segment_code == "UNKNOWN"


def test_bounded_generation_source_dimensions_are_extracted_and_cached(tmp_path, monkeypatch):
    html = (
        "<html><body>Длина кузова, ширина и высота составляют 4680, 1900 и 1700 мм. "
        "Колёсная база составляет 2780 мм.</body></html>"
    )
    assert dimensions_from_catalog_html(html) == {"length_mm": 4680, "wheelbase_mm": 2780}
    calls = 0

    async def fake_get(self, url):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            text=html,
            request=httpx.Request("GET", url),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    cache = catalog(
        tmp_path,
        source_refs=[{"source": "drom.ru", "url": "https://www.drom.ru/catalog/example/gen/"}],
    )
    path = tmp_path / "segments.db"
    first = asyncio.run(
        SegmentClassifier(cache, path).classify(
            "Example Brand",
            "Example Model",
            "generation-1",
            BodyType.CROSSOVER,
            allow_source_fetch=True,
        )
    )
    second = asyncio.run(
        SegmentClassifier(cache, path).classify(
            "Example Brand",
            "Example Model",
            "generation-1",
            BodyType.CROSSOVER,
            allow_source_fetch=True,
        )
    )
    assert first.segment_code == "J-C" and first.segment_method == "dimensions"
    assert second.cache_hit and calls == 1


def test_ambiguous_ai_fallback_is_cached_and_provider_failure_is_safe(tmp_path):
    calls = 0

    async def answer(metadata, body):
        nonlocal calls
        calls += 1
        assert metadata["category"] == "suv" and body == BodyType.CROSSOVER
        return SegmentClassification("J-C", "suv", "C", "unknown")

    cache = catalog(
        tmp_path,
        model_classification={
            "category": "suv",
            "dimensions": {"length_mm": 4400, "wheelbase_mm": 3100},
        },
    )
    path = tmp_path / "segments.db"
    classifier = SegmentClassifier(cache, path, answer)
    first = asyncio.run(
        classifier.classify(
            "Example Brand", "Example Model", None, BodyType.CROSSOVER, allow_ai=True
        )
    )
    second = asyncio.run(
        SegmentClassifier(cache, path, answer).classify(
            "Example Brand", "Example Model", None, BodyType.CROSSOVER, allow_ai=True
        )
    )
    assert first.segment_method == "ai_fallback" and first.ai_fallback_called
    assert second.cache_hit and calls == 1

    async def failing(metadata, body):
        raise RuntimeError("provider unavailable")

    failed = asyncio.run(
        SegmentClassifier(cache, tmp_path / "failed.db", failing).classify(
            "Example Brand", "Example Model", None, BodyType.CROSSOVER, allow_ai=True
        )
    )
    assert failed.segment_code == "UNKNOWN" and failed.ai_fallback_called


def listing(external_id: str, price: int, segment_code: str) -> CarListing:
    return CarListing(
        source="example.test",
        external_id=external_id,
        brand="Other Brand",
        model="Other Model",
        year=2020,
        body_type=BodyType.CROSSOVER,
        price=price,
        url=f"https://example.test/{external_id}",
        checked_at=datetime.now(UTC),
        segment="suv",
        segment_code=segment_code,
    )


def test_direct_uses_class_and_price_unknown_falls_back():
    engine = CompetitorEngine()
    source = Car(
        brand="Example Brand",
        model="Example Model",
        year=2020,
        body_type=BodyType.CROSSOVER,
        price=1_000_000,
        segment="suv",
        segment_code="J-C",
    )
    candidates = [
        listing("distant", 1_000_000, "J-F"),
        listing("same", 1_050_000, "J-C"),
        listing("unknown", 1_020_000, "UNKNOWN"),
        listing("expensive", 1_150_000, "J-C"),
        listing("cheaper", 850_000, "J-C"),
    ]
    groups = engine.classify(source, candidates)
    assert [item.listing.external_id for item in groups.direct] == ["same", "unknown"]
    assert [item.listing.external_id for item in groups.expensive] == ["expensive"]
    assert [item.listing.external_id for item in groups.cheaper] == ["cheaper"]
    assert engine.classify_price(1_000_000, 1_070_000).value == "direct"
    assert engine.classify_price(1_000_000, 1_070_001).value == "expensive"


def test_provider_failure_keeps_marketplace_search_usable(tmp_path):
    async def failing(metadata, body):
        raise RuntimeError("provider unavailable")

    class SyntheticScraper(BaseScraper):
        source = "example.test"

        async def search(self, query):
            return [listing("candidate", 1_000_000, "UNKNOWN")]

    cache = catalog(
        tmp_path,
        generation_classification={
            "category": "suv",
            "dimensions": {"length_mm": 4400, "wheelbase_mm": 3100},
        },
    )
    service = SearchService(
        scrapers=[SyntheticScraper()],
        marketplace_catalog=cache,
        segment_classifier=SegmentClassifier(cache, tmp_path / "segments.db", failing),
    )
    result = asyncio.run(
        service.search(
            SearchRequest(
                brand="Example Brand",
                model="Example Model",
                generation_id="generation-1",
                year=2020,
                body_type="suv",
                price=1_000_000,
                region="any",
                debug=True,
            )
        )
    )
    assert result.pipeline_diagnostics["segment_ai_fallback_called"] is True
    assert result.source_vehicle.segment_code == "UNKNOWN"
    assert len(result.direct.listings) == 1
