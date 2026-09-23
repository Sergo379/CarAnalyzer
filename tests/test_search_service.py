import asyncio
from datetime import UTC, datetime

import pytest

from backend.models.car import BodyType, MarketDiscoveryRequest, SearchRequest
from backend.models.listing import CarListing
from backend.scrapers.base import BaseScraper, Http429Error, SourceBlockedError
from backend.services.marketplace_catalog import CatalogCache
from backend.services.search_service import SearchService, SourceState


class StaticScraper(BaseScraper):
    def __init__(self, source: str, listings: list[CarListing]) -> None:
        self.source = source
        self.listings = listings

    async def search(self, query: SearchRequest) -> list[CarListing]:
        return self.listings


class BlockedScraper(BaseScraper):
    source = "avito"

    async def search(self, query: SearchRequest) -> list[CarListing]:
        raise SourceBlockedError("blocked in test")


class RateLimitedScraper(BaseScraper):
    source = "drom.ru"

    async def search(self, query: SearchRequest) -> list[CarListing]:
        raise Http429Error("rate limited")


class SlowScraper(BaseScraper):
    source = "slow"

    def __init__(self) -> None:
        from backend.config import Settings

        self.settings = Settings(
            _env_file=None, source_search_timeout_seconds=0.01, search_total_timeout_seconds=0.1
        )

    async def search(self, query: SearchRequest) -> list[CarListing]:
        await asyncio.sleep(1)
        return []


class BroadScraper(BaseScraper):
    source = "broad"

    def __init__(self, listings: list[CarListing]) -> None:
        self.listings = listings
        self.request: MarketDiscoveryRequest | None = None

    async def search(self, query: SearchRequest) -> list[CarListing]:
        raise AssertionError("model-specific search must not drive candidate discovery")

    async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
        self.request = query
        return self.listings


class SourceAndBroadScraper(BroadScraper):
    def __init__(self, source_listings: list[CarListing], discovered: list[CarListing]) -> None:
        super().__init__(discovered)
        self.source_listings = source_listings

    async def search(self, query: SearchRequest) -> list[CarListing]:
        return self.source_listings


class BrokenCatalog:
    def classify(self, *args: object) -> object:
        raise RuntimeError("catalog unavailable")


def listing(source: str, external_id: str, price: int) -> CarListing:
    return CarListing(
        source=source,
        external_id=external_id,
        brand="Audi",
        model="A6",
        year=2022,
        body_type="sedan",
        price=price,
        url=f"https://example.com/{source}/{external_id}",
        checked_at=datetime.now(UTC),
    )


def synthetic_listing(
    source: str,
    external_id: str,
    brand: str,
    model: str,
    *,
    year: int = 2022,
    location: str | None = None,
) -> CarListing:
    return CarListing(
        source=source,
        external_id=external_id,
        brand=brand,
        model=model,
        year=year,
        body_type="sedan",
        price=1_000_000,
        url=f"https://example.test/{source}/{external_id}",
        location=location,
        checked_at=datetime.now(UTC),
    )


def test_search_service_injects_one_catalog_cache_into_all_readers(tmp_path, monkeypatch) -> None:
    import backend.services.marketplace_catalog as catalog_module

    path = tmp_path / "catalog.json"
    path.write_text('{"version":3,"sources":{},"brands":[]}', encoding="utf-8")
    cache = CatalogCache(path=path)
    loads = 0
    original = catalog_module.json.loads

    def counted_loads(value: str):
        nonlocal loads
        loads += 1
        return original(value)

    monkeypatch.setattr(catalog_module.json, "loads", counted_loads)
    service = SearchService(marketplace_catalog=cache)
    assert service.catalog.cache is cache
    assert service.marketplace_catalog is cache
    assert service.scrapers[0].catalog is cache  # Auto.ru
    assert service.scrapers[1].catalog is cache  # Drom
    service.catalog.cache.load()
    service.marketplace_catalog.load()
    service.scrapers[0].catalog.load()
    service.scrapers[1].catalog.load()
    assert loads == 1


@pytest.mark.parametrize(
    ("price", "category"),
    [(4_100_000, "direct"), (4_700_000, "expensive"), (3_500_000, "cheaper")],
)
def test_search_service_classifies_and_groups(price: int, category: str) -> None:
    service = SearchService(scrapers=[StaticScraper("auto.ru", [listing("auto.ru", "1", price)])])
    request = SearchRequest(
        brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000
    )
    result = asyncio.run(service.search(request))
    selected = getattr(result, category)
    assert len(selected.listings) == 1
    assert selected.model_groups[0].listings_count == 1
    assert selected.model_groups[0].average_price == price


def test_partial_source_failure_keeps_successful_data() -> None:
    service = SearchService(
        scrapers=[
            StaticScraper("auto.ru", [listing("auto.ru", "1", 4_100_000)]),
            BlockedScraper(),
        ]
    )
    request = SearchRequest(
        brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000
    )
    result = asyncio.run(service.search(request))
    assert result.source_status == {"auto.ru": SourceState.OK, "avito": SourceState.BLOCKED}
    assert len(result.direct.listings) == 1
    assert result.warnings


def test_auto_failure_does_not_stop_drom() -> None:
    class FailedAuto(BaseScraper):
        source = "auto.ru"

        async def search(self, query: SearchRequest) -> list[CarListing]:
            raise SourceBlockedError("unavailable")

    result = asyncio.run(
        SearchService(
            scrapers=[
                StaticScraper("drom.ru", [listing("drom.ru", "1", 4_100_000)]),
                FailedAuto(),
            ]
        ).search(
            SearchRequest(brand="Audi", model="A6", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert result.source_status["auto.ru"] == SourceState.BLOCKED
    assert result.source_status["drom.ru"] == SourceState.OK
    assert len(result.source_listings.listings) == 1


def test_avito_restriction_preserves_auto_and_drom_results() -> None:
    result = asyncio.run(
        SearchService(
            scrapers=[
                BlockedScraper(),
                StaticScraper("drom.ru", [listing("drom.ru", "2", 4_100_000)]),
                StaticScraper("auto.ru", [listing("auto.ru", "1", 4_100_000)]),
            ]
        ).search(
            SearchRequest(brand="Audi", model="A6", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert result.source_status["avito"] == SourceState.BLOCKED
    assert result.source_status["auto.ru"] == SourceState.OK
    assert result.source_status["drom.ru"] == SourceState.OK
    assert len(result.source_listings.listings) == 2
    assert result.source_operations["auto.ru"]["target"].count == 1
    assert result.source_operations["drom.ru"]["target"].count == 1


@pytest.mark.parametrize(
    ("brand", "model"), [("Example Make", "Alpha+"), ("Second Make", "Beta-X")]
)
def test_valid_target_count_matches_final_source_collection(brand: str, model: str) -> None:
    scraper = SourceAndBroadScraper(
        [synthetic_listing("target.test", "1", brand, model)], []
    )
    result = asyncio.run(
        SearchService(scrapers=[scraper]).search(
            SearchRequest(
                brand=brand,
                model=model,
                year=2022,
                body_type="any",
                transmission="any",
                region="any",
                price=1_000_000,
            )
        )
    )
    assert len(result.source_listings.listings) == 1
    assert result.source_operations["broad"]["target"].count == 1
    assert result.source_distribution["target.test"] == 1
    assert not result.direct.listings


def test_rejected_target_is_not_reported_as_returned_market_count() -> None:
    scraper = SourceAndBroadScraper(
        [synthetic_listing("target.test", "wrong-year", "Example Make", "Alpha", year=2020)],
        [],
    )
    result = asyncio.run(
        SearchService(scrapers=[scraper]).search(
            SearchRequest(
                brand="Example Make",
                model="Alpha",
                year=2022,
                body_type="any",
                region="any",
                price=1_000_000,
            )
        )
    )
    assert not result.source_listings.listings
    assert result.source_operations["broad"]["target"].count == 0
    assert result.source_operations["broad"]["target"].state == SourceState.EMPTY


def test_primary_sources_overlap_and_avito_starts_after_both_finish() -> None:
    starts: list[str] = []
    primary_started: set[str] = set()
    release_primary = asyncio.Event()

    class RecordingScraper(BaseScraper):
        def __init__(self, source: str) -> None:
            self.source = source

        async def search(self, query: SearchRequest) -> list[CarListing]:
            starts.append(self.source)
            if self.source != "avito":
                primary_started.add(self.source)
                if primary_started == {"auto.ru", "drom.ru"}:
                    release_primary.set()
                await release_primary.wait()
            else:
                assert primary_started == {"auto.ru", "drom.ru"}
            return []

        async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
            return []

    service = SearchService(
        scrapers=[
            RecordingScraper("avito"),
            RecordingScraper("drom.ru"),
            RecordingScraper("auto.ru"),
        ]
    )
    result = asyncio.run(
        service.search(
            SearchRequest(brand="Audi", model="A6", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert set(starts[:2]) == {"auto.ru", "drom.ru"}
    assert starts[-1] == "avito"
    assert list(result.source_status) == ["auto.ru", "drom.ru", "avito"]


def test_failed_middle_source_does_not_prevent_later_source_or_discard_earlier_data() -> None:
    starts: list[str] = []

    class RecordingScraper(BaseScraper):
        def __init__(self, source: str, fail: bool = False) -> None:
            self.source = source
            self.fail = fail

        async def search(self, query: SearchRequest) -> list[CarListing]:
            starts.append(self.source)
            if self.fail:
                raise SourceBlockedError("blocked in test")
            return [listing(self.source, "1", 4_100_000)]

        async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
            if self.fail:
                raise SourceBlockedError("blocked in test")
            return []

    result = asyncio.run(
        SearchService(
            scrapers=[
                RecordingScraper("avito"),
                RecordingScraper("drom.ru", fail=True),
                RecordingScraper("auto.ru"),
            ]
        ).search(
            SearchRequest(brand="Audi", model="A6", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert starts == ["auto.ru", "drom.ru", "avito"]
    assert result.source_status["drom.ru"] == SourceState.BLOCKED
    assert result.source_status["auto.ru"] == SourceState.OK
    assert result.source_status["avito"] == SourceState.OK
    assert len(result.source_listings.listings) == 2


def test_stalled_auto_does_not_cancel_concurrent_drom() -> None:
    from backend.config import Settings

    starts: list[str] = []

    class RecordingScraper(BaseScraper):
        def __init__(self, source: str) -> None:
            self.source = source
            self.settings = Settings(
                _env_file=None,
                source_search_timeout_seconds=0.09,
                search_total_timeout_seconds=0.09,
            )

        async def search(self, query: SearchRequest) -> list[CarListing]:
            starts.append(self.source)
            if self.source == "auto.ru":
                await asyncio.sleep(0.2)
            return []

        async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
            if self.source == "auto.ru":
                await asyncio.sleep(0.2)
            return []

    result = asyncio.run(
        SearchService(
            scrapers=[
                RecordingScraper("avito"),
                RecordingScraper("drom.ru"),
                RecordingScraper("auto.ru"),
            ]
        ).search(
            SearchRequest(brand="Audi", model="A6", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert "drom.ru" in starts
    assert result.source_status["auto.ru"] == SourceState.TIMEOUT
    assert result.source_status["drom.ru"] == SourceState.EMPTY
    assert result.source_status["avito"] == SourceState.TIMEOUT


def test_source_timeout_keeps_completed_sources() -> None:
    result = asyncio.run(
        SearchService(
            scrapers=[SlowScraper(), StaticScraper("auto.ru", [listing("auto.ru", "1", 4_100_000)])]
        ).search(
            SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert result.source_status["slow"] == SourceState.TIMEOUT
    assert result.source_status["auto.ru"] == SourceState.OK


def test_source_timeout_preserves_completed_child() -> None:
    from backend.config import Settings

    class PartialTimeoutScraper(BaseScraper):
        source = "partial-timeout"

        def __init__(self) -> None:
            self.settings = Settings(
                _env_file=None,
                source_search_timeout_seconds=0.01,
                search_total_timeout_seconds=0.1,
            )

        async def search(self, query: SearchRequest) -> list[CarListing]:
            return [listing(self.source, "target", 4_100_000)]

        async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
            await asyncio.sleep(1)
            return []

    result = asyncio.run(
        SearchService(scrapers=[PartialTimeoutScraper()]).search(
            SearchRequest(brand="Audi", model="A6", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert result.source_status == {"partial-timeout": SourceState.PARTIAL}
    assert len(result.source_listings.listings) == 1
    assert result.source_operations["partial-timeout"]["target"].count == 1
    assert result.source_operations["partial-timeout"]["competitors"].state == SourceState.TIMEOUT
    assert result.source_operations["partial-timeout"]["competitors"].error_type == "TimeoutError"


def test_http_429_has_precise_source_status() -> None:
    result = asyncio.run(
        SearchService(scrapers=[RateLimitedScraper()]).search(
            SearchRequest(
                brand="Ford", model="Fiesta", year=2016, body_type="hatchback", price=700_000
            )
        )
    )
    assert result.source_status == {"drom.ru": SourceState.HTTP_429}
    assert "HTTP 429" in result.source_details["drom.ru"]


def test_unknown_ford_uses_broad_price_body_discovery_and_price_700000() -> None:
    candidate = CarListing(
        source="broad",
        external_id="ford-1",
        brand="Toyota",
        model="Yaris",
        modification="1.5 CVT",
        year=2017,
        body_type="hatchback",
        price=700_000,
        url="https://example.com/broad/ford-1",
        checked_at=datetime.now(UTC),
    )
    scraper = BroadScraper([candidate])
    result = asyncio.run(
        SearchService(scrapers=[scraper]).search(
            SearchRequest(
                brand="Ford",
                model="Fiesta",
                year=2016,
                body_type="hatchback",
                price=700_000,
            )
        )
    )
    assert scraper.request is not None
    assert scraper.request.price_from == 560_000
    assert scraper.request.price_to == 840_000
    assert scraper.request.body_types == frozenset({"hatchback", "liftback"})
    assert result.source_vehicle.model == "Fiesta"
    assert result.source_vehicle.segment == "passenger"
    assert result.direct.listings[0].listing.model == "Yaris"
    assert "ford:fiesta" in result.car_knowledge
    assert "toyota:yaris" in result.car_knowledge


def test_catalog_failure_does_not_break_market_discovery() -> None:
    scraper = BroadScraper([listing("broad", "1", 4_100_000)])
    service = SearchService(scrapers=[scraper], catalog=BrokenCatalog())  # type: ignore[arg-type]
    result = asyncio.run(
        service.search(
            SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert result.source_vehicle.segment is None
    assert len(result.direct.listings) == 1


def test_unknown_generation_does_not_reject_valid_target_listing(tmp_path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(
        '{"version":3,"sources":{},"brands":[{"id":"make","name":"Make",'
        '"models":[{"id":"make:model","name":"Model","generations":['
        '{"id":"make:model:g1","name":"G1","year_from":2020,"year_to":2024}]}]}]}',
        encoding="utf-8",
    )
    cache = CatalogCache(path=path)
    service = SearchService(scrapers=[], marketplace_catalog=cache)
    query = SearchRequest(
        brand="Make",
        model="Model",
        generation_id="make:model:g1",
        year=2022,
        body_type="sedan",
        price=1_000_000,
    )
    candidate = CarListing(
        source="test",
        external_id="1",
        brand="Make",
        model="Model",
        canonical_model_id="make:model",
        year=2022,
        body_type="sedan",
        price=1_000_000,
        url="https://example.test/1",
        checked_at=datetime.now(UTC),
    )
    assert service._source_rejection_reason(candidate, query) is None


def test_body_any_discovers_all_types_without_compatibility_filter() -> None:
    candidate = CarListing(
        source="broad",
        external_id="any-body",
        brand="Toyota",
        model="RAV4",
        year=2018,
        body_type="crossover",
        price=4_100_000,
        url="https://example.com/any-body",
        checked_at=datetime.now(UTC),
    )
    scraper = BroadScraper([candidate])
    result = asyncio.run(
        SearchService(scrapers=[scraper]).search(
            SearchRequest(
                brand="BMW",
                model="X5",
                year=2018,
                body_type="any",
                price=4_100_000,
            )
        )
    )
    assert scraper.request is not None
    assert scraper.request.body_types == frozenset(BodyType)
    assert result.source_vehicle.body_type == "any"
    assert result.direct.listings[0].listing.body_type == "crossover"
    assert len(result.direct.listings) == 1


def test_source_model_listings_are_separate_with_statistics() -> None:
    source_listing = CarListing(
        source="auto.ru",
        external_id="source-1",
        brand="BMW",
        model="5 Series",
        modification="520i AT",
        year=2022,
        body_type="sedan",
        transmission="automatic",
        price=4_000_000,
        url="https://example.com/auto.ru/source-1",
        location="Москва",
        checked_at=datetime.now(UTC),
    )
    scraper = SourceAndBroadScraper(
        [source_listing], [source_listing, listing("broad", "competitor", 4_100_000)]
    )
    result = asyncio.run(
        SearchService(scrapers=[scraper]).search(
            SearchRequest(
                brand="BMW",
                model="520i",
                year=2022,
                body_type="sedan",
                transmission="automatic",
                region="moscow_and_oblast",
                price=4_100_000,
            )
        )
    )
    assert len(result.source_listings.listings) == 1
    assert result.source_listings.listings[0].price_difference == -100_000
    assert result.source_model_group is not None
    assert result.source_model_group.average_price == 4_000_000
    assert result.source_distribution == {"broad": 0, "auto.ru": 1}
    assert all(item.listing.model != "5 Series" for item in result.direct.listings)


def test_strict_transmission_and_region_exclude_unknown_and_vladivostok() -> None:
    candidates = [
        CarListing(
            source="broad",
            external_id="vladivostok",
            brand="Audi",
            model="A6",
            year=2020,
            body_type="sedan",
            transmission="automatic",
            price=4_100_000,
            url="https://example.com/vladivostok",
            location="Владивосток",
            checked_at=datetime.now(UTC),
        ),
        CarListing(
            source="broad",
            external_id="unknown-transmission",
            brand="Audi",
            model="A6",
            year=2020,
            body_type="sedan",
            price=4_100_000,
            url="https://example.com/unknown",
            location="Москва",
            checked_at=datetime.now(UTC),
        ),
        CarListing(
            source="broad",
            external_id="moscow-auto",
            brand="Audi",
            model="A6",
            year=2020,
            body_type="sedan",
            transmission="automatic",
            price=4_100_000,
            url="https://example.com/moscow",
            location="Москва",
            checked_at=datetime.now(UTC),
        ),
    ]
    result = asyncio.run(
        SearchService(scrapers=[BroadScraper(candidates)]).search(
            SearchRequest(
                brand="BMW",
                model="520i",
                year=2022,
                body_type="sedan",
                transmission="automatic",
                region="moscow_and_oblast",
                price=4_100_000,
            )
        )
    )
    assert [item.listing.external_id for item in result.direct.listings] == ["moscow-auto"]


def test_target_success_discovery_failure_preserves_target() -> None:
    class TargetOnlyScraper(BaseScraper):
        source = "target-only"

        async def search(self, query: SearchRequest) -> list[CarListing]:
            return [listing(self.source, "target", 4_100_000)]

        async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
            raise Http429Error("discovery limited")

    result = asyncio.run(
        SearchService(scrapers=[TargetOnlyScraper()]).search(
            SearchRequest(brand="Audi", model="A6", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert result.source_status == {"target-only": SourceState.PARTIAL}
    assert len(result.source_listings.listings) == 1


def test_discovery_success_target_failure_preserves_competitors() -> None:
    class DiscoveryOnlyScraper(BaseScraper):
        source = "discovery-only"

        async def search(self, query: SearchRequest) -> list[CarListing]:
            raise Http429Error("target limited")

        async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
            return [listing(self.source, "competitor", 4_100_000)]

    result = asyncio.run(
        SearchService(scrapers=[DiscoveryOnlyScraper()]).search(
            SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert result.source_status == {"discovery-only": SourceState.PARTIAL}
    assert len(result.direct.listings) == 1


def test_knowledge_is_unique_per_canonical_model() -> None:
    candidates = [
        listing("one", "1", 4_100_000),
        listing("two", "2", 4_100_000),
    ]
    result = asyncio.run(
        SearchService(scrapers=[BroadScraper(candidates)]).search(
            SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)
        )
    )
    assert list(result.car_knowledge).count("audi:a6") == 1
    assert "bmw:5series" in result.car_knowledge
    assert len(result.car_knowledge) == 2


def test_different_models_keep_different_canonical_ids() -> None:
    service = SearchService(scrapers=[])
    ids = {
        service.marketplace_catalog.resolve_identity(brand, model).canonical_model_id
        for brand, model in (("Ford", "Fiesta"), ("Volkswagen", "Polo"), ("Skoda", "Fabia"))
    }
    assert len(ids) == 3
