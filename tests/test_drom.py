import asyncio

import httpx

from backend.config import Settings
from backend.models.car import MarketDiscoveryRequest, SearchRequest
from backend.models.catalog import CanonicalVehicleIdentity, SourceReference
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


def test_drom_http_429_uses_isolated_browser_fallback() -> None:
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
    listings = asyncio.run(scraper.search(QUERY))
    assert len(listings) == 1
    assert calls == [scraper.build_search_url(QUERY)]


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


def test_drom_follows_next_page_and_stops_without_it() -> None:
    first_page = search_html().replace(
        b"</body>", b'<a rel="next" href="/bmw/5-series/page2/">Next</a></body>'
    )
    second_page = (
        search_html().replace(b"837871166", b"837871169").replace(b"837871167", b"837871170")
    )

    def handler(request: httpx.Request) -> httpx.Response:
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


def test_drom_uses_native_year_and_price_ranges() -> None:
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
    assert "/year-2019-2022/" in url
    assert "minprice=3000000&maxprice=4000000" in url
