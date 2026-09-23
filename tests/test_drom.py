import asyncio

import httpx

from backend.config import Settings
from backend.models.car import BodyFilter, BodyType, MarketDiscoveryRequest, SearchRequest
from backend.models.catalog import CanonicalVehicleIdentity, SourceReference
from backend.scrapers.base import BrowserAccessLimitedError, Http429Error
from backend.scrapers.drom import DromScraper

QUERY = SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)


class FakeCatalog:
    def resolve_identity(self, brand: str, model: str) -> CanonicalVehicleIdentity:
        return CanonicalVehicleIdentity(
            canonical_brand_id="bmw",
            canonical_model_id="bmw:5series",
            brand="BMW",
            model="5-Series",
        )

    def source_model_ref(self, source: str, brand: str, model: str) -> SourceReference | None:
        assert (source, brand, model) == ("drom.ru", "BMW", "5-Series")
        return SourceReference(
            source="drom.ru",
            url="https://www.drom.ru/catalog/bmw/5-series/",
            path="/catalog/bmw/5-series/",
            slug="5-series",
        )

    def generations(
        self,
        brand: str,
        model: str,
        year: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> list[dict[str, object]]:
        return [{"body_types": ["sedan"], "modifications": []}]


class EmptyGenerationCatalog(FakeCatalog):
    def generations(self, *args, **kwargs):
        return []


def search_html() -> bytes:
    return """
    <html><body>
      <div data-ftid="bulls-list_bull">
        <a data-ftid="bull_title"
           href="https://auto.drom.ru/moscow/bmw/5-series/837871166.html">
          BMW 5-Series, 2022
        </a>
        <div data-ftid="bull_subtitle">520i AT M Sport</div>
        <span data-ftid="bull_price">4 850 000 ₽</span>
        <span data-ftid="bull_location">Москва</span>
      </div>
      <div data-ftid="bulls-list_bull">
        <a data-ftid="bull_title"
           href="https://auto.drom.ru/moscow/bmw/5-series/837871167.html">
          BMW 5-Series, 2022
        </a>
        <div data-ftid="bull_subtitle">530i AT M Sport</div>
        <span data-ftid="bull_price">5 200 000 ₽</span>
        <span data-ftid="bull_location">Москва</span>
      </div>
      <section data-ftid="bulletin-list_archive">
        <div data-ftid="bulls-list_bull">
          <a data-ftid="bull_title"
             href="https://auto.drom.ru/moscow/bmw/5-series/837871168.html">
            BMW 5-Series, 2022
          </a>
          <div data-ftid="bull_subtitle">520i AT Luxury</div>
          <span data-ftid="bull_price">4 100 000 ₽</span>
        </div>
      </section>
    </body></html>
    """.encode()


def test_drom_parses_matching_active_cards_only() -> None:
    listings = DromScraper.parse_search(search_html(), QUERY)
    assert len(listings) == 1
    assert listings[0].external_id == "837871166"
    assert listings[0].price == 4_850_000
    assert listings[0].location == "Москва"
    assert listings[0].transmission == "automatic"


def test_drom_search_uses_verified_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=search_html(), request=request)

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    listings = asyncio.run(scraper.search(QUERY))
    assert len(listings) == 1
    assert scraper.build_search_url(QUERY).endswith(
        "/bmw/5-series/year-2022/used/sedan/?unsold=1&minprice=3280000&maxprice=4920000"
    )


def test_drom_broad_discovery_url_and_parser() -> None:
    discovery = MarketDiscoveryRequest(
        source_vehicle=QUERY,
        price_from=3_280_000,
        price_to=4_920_000,
        body_types={"sedan"},
    )
    url = DromScraper.build_discovery_url(discovery, QUERY.body_type)
    assert url == ("https://auto.drom.ru/sedan/?minprice=3280000&maxprice=4920000&unsold=1")
    listings = DromScraper.parse_discovery(search_html(), QUERY.body_type)
    assert len(listings) == 2
    assert listings[0].model == "5 Series"
    assert listings[0].modification == "520i AT M Sport"


def test_drom_combined_region_uses_moscow_distance_query() -> None:
    query = SearchRequest(
        brand="Renault",
        model="Captur",
        year=2017,
        body_type="crossover",
        transmission="automatic",
        region="moscow_and_oblast",
        price=1_000_000,
    )
    discovery = MarketDiscoveryRequest(
        source_vehicle=query,
        price_from=800_000,
        price_to=1_200_000,
        body_types={"crossover", "suv"},
    )
    url = DromScraper.build_discovery_url(discovery, query.body_type)
    assert url.startswith("https://auto.drom.ru/moscow/suv/")
    assert "distance=100" in url


def test_drom_http_429_stops_without_browser_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"automation limited", request=request)

    calls: list[str] = []

    async def browser_loader(url: str) -> bytes:
        calls.append(url)
        return search_html()

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
        browser_loader=browser_loader,
    )
    try:
        asyncio.run(scraper.search(QUERY))
    except Http429Error:
        pass
    else:
        raise AssertionError("Expected Drom rate limit")
    assert calls == []
    assert scraper.combined_diagnostics().http_requests == 1


def test_drom_browser_limit_opens_circuit_for_parallel_operations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"access limited", request=request)

    calls: list[str] = []

    async def browser_loader(url: str) -> bytes:
        calls.append(url)
        raise BrowserAccessLimitedError("browser limited")

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
        browser_loader=browser_loader,
    )
    discovery = MarketDiscoveryRequest(
        source_vehicle=QUERY,
        price_from=3_280_000,
        price_to=4_920_000,
        body_types={"sedan"},
    )

    async def run() -> None:
        results = await asyncio.gather(
            scraper.search(QUERY), scraper.discover(discovery), return_exceptions=True
        )
        assert all(isinstance(value, BrowserAccessLimitedError) for value in results)

    asyncio.run(run())
    assert len(calls) == 1
    assert scraper.combined_diagnostics().browser_fallbacks == 1


def test_drom_any_body_omits_body_path() -> None:
    query = QUERY.model_copy(update={"body_type": "any"})
    scraper = DromScraper(catalog=FakeCatalog())  # type: ignore[arg-type]
    url = scraper.build_search_url(query)
    assert "/used/?unsold=1" in url


def test_drom_any_body_uses_unambiguous_catalog_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=search_html(), request=request)

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    listings = asyncio.run(scraper.search(QUERY.model_copy(update={"body_type": "any"})))
    assert len(listings) == 1
    assert listings[0].body_type == "sedan"


def test_drom_any_body_retains_listing_when_catalog_and_card_body_are_unknown() -> None:
    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=search_html(), request=request)
        ),
        catalog=EmptyGenerationCatalog(),  # type: ignore[arg-type]
    )
    listings = asyncio.run(scraper.search(QUERY.model_copy(update={"body_type": "any"})))
    assert len(listings) == 1
    assert listings[0].body_type is None
    assert scraper.diagnostics["target"].http_requests == 1


def test_selected_engine_id_does_not_enter_model_matcher() -> None:
    query = QUERY.model_copy(update={"model": "5-Series", "modification_id": "engine:identity"})
    listings = DromScraper.parse_search(search_html(), query, expected_model_slug="5-series")
    assert len(listings) == 2


def test_discovery_keeps_successful_routes_after_later_parse_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/suv/" in request.url.path:
            return httpx.Response(500, content=b"unavailable", request=request)
        content = search_html()
        if "/wagon/" in request.url.path:
            content = content.replace(b"837871166", b"837871169")
        return httpx.Response(200, content=content, request=request)

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    discovery = MarketDiscoveryRequest(
        source_vehicle=QUERY,
        price_from=3_280_000,
        price_to=4_920_000,
        body_types={"sedan", "suv", "wagon"},
    )
    listings = asyncio.run(scraper.discover(discovery))
    assert {item.external_id for item in listings} == {"837871166", "837871169"}
    diagnostic = scraper.diagnostics["competitors"]
    assert diagnostic.degraded
    assert diagnostic.partial_failures == 1
    assert diagnostic.http_requests == 3


def test_target_keeps_successful_route_if_another_route_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/wagon/" in request.url.path:
            return httpx.Response(500, content=b"unavailable", request=request)
        return httpx.Response(200, content=search_html(), request=request)

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    scraper._target_body_queries = lambda query: [  # type: ignore[method-assign]
        query,
        query.model_copy(update={"body_type": BodyFilter.WAGON}),
    ]
    listings = asyncio.run(scraper.search(QUERY))
    assert {item.external_id for item in listings} == {"837871166"}
    assert scraper.diagnostics["target"].partial_failures == 1
    assert scraper.diagnostics["target"].degraded


def test_duplicate_drom_body_paths_fetch_once() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=search_html(), request=request)

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    discovery = MarketDiscoveryRequest(
        source_vehicle=QUERY,
        price_from=3_280_000,
        price_to=4_920_000,
        body_types={"crossover", "suv"},
    )
    asyncio.run(scraper.discover(discovery))
    assert len(calls) == 1


def test_body_any_discovery_uses_one_broad_route() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=search_html(), request=request)

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    discovery = MarketDiscoveryRequest(
        source_vehicle=QUERY.model_copy(update={"body_type": BodyFilter.ANY}),
        price_from=3_280_000,
        price_to=4_920_000,
        body_types=frozenset(BodyType),
    )
    listings = asyncio.run(scraper.discover(discovery))
    assert len(calls) == 1
    assert calls[0].startswith("https://auto.drom.ru/?")
    assert len(listings) == 1


def test_slow_target_request_does_not_hold_discovery_request_lock() -> None:
    target_started = asyncio.Event()
    release_target = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/bmw/" in request.url.path:
            target_started.set()
            await release_target.wait()
        return httpx.Response(200, content=search_html(), request=request)

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    discovery = MarketDiscoveryRequest(
        source_vehicle=QUERY,
        price_from=3_280_000,
        price_to=4_920_000,
        body_types={"sedan"},
    )

    async def run() -> None:
        target = asyncio.create_task(scraper.search(QUERY))
        try:
            await asyncio.wait_for(target_started.wait(), timeout=0.2)
            competitors = await asyncio.wait_for(scraper.discover(discovery), timeout=0.2)
            assert competitors
        finally:
            release_target.set()
            await target

    asyncio.run(run())


def test_malformed_target_card_is_rejected_individually() -> None:
    malformed = b'<div data-ftid="bulls-list_bull"><span>broken</span></div>'
    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=search_html().replace(b"</body>", malformed + b"</body>"),
                request=request,
            )
        ),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    listings = asyncio.run(scraper.search(QUERY))
    assert len(listings) == 1
    assert scraper.diagnostics["target"].rejected["parse"] == 1


def test_rate_limit_circuit_prevents_target_discovery_request_storm() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(429, content=b"limited", request=request)

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    discovery = MarketDiscoveryRequest(
        source_vehicle=QUERY,
        price_from=3_280_000,
        price_to=4_920_000,
        body_types={"sedan", "wagon", "suv"},
    )

    async def run():
        return await asyncio.gather(
            scraper.search(QUERY), scraper.discover(discovery), return_exceptions=True
        )

    results = asyncio.run(run())
    assert all(isinstance(item, Http429Error) for item in results)
    assert len(calls) == 1


def test_drom_live_search_stops_after_bounded_second_page() -> None:
    first_page = search_html().replace(
        b"</body>", b'<a rel="next" href="/bmw/5-series/page2/">Next</a></body>'
    )
    second_page = (
        search_html().replace(b"837871166", b"837871169").replace(b"837871167", b"837871170")
        .replace(b"</body>", b'<a rel="next" href="/bmw/5-series/page3/">Next</a></body>')
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        content = second_page if "page2" in request.url.path else first_page
        return httpx.Response(200, content=content, request=request)

    scraper = DromScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    listings = asyncio.run(scraper.search(QUERY))
    assert {item.external_id for item in listings} == {"837871166", "837871169"}
    assert scraper.combined_diagnostics().pages_scanned == 2
    assert not any("page3" in url for url in calls)


def test_drom_uses_supported_year_path_and_native_price_range() -> None:
    query = SearchRequest(
        brand="BMW",
        model="520i",
        year_mode="range",
        year_from=2019,
        year_to=2022,
        price_mode="range",
        price_from=3_000_000,
        price_to=4_000_000,
    )
    url = DromScraper(catalog=FakeCatalog()).build_search_url(query)  # type: ignore[arg-type]
    assert "/bmw/5-series/used/" in url
    assert "/year-" not in url
    assert "minprice=3000000&maxprice=4000000" in url

    exact = query.model_copy(update={"year_mode": "exact", "year": 2020})
    assert "/year-2020/used/" in DromScraper(catalog=FakeCatalog()).build_search_url(exact)  # type: ignore[arg-type]
