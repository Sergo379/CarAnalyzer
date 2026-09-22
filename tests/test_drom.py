import asyncio

import httpx

from backend.config import Settings
from backend.models.car import MarketDiscoveryRequest, SearchRequest
from backend.scrapers.drom import DromScraper

QUERY = SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)


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

    scraper = DromScraper(settings=Settings(_env_file=None), transport=httpx.MockTransport(handler))
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
