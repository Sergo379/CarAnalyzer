import asyncio
import json

import httpx

from backend.config import Settings
from backend.models.car import MarketDiscoveryRequest, SearchRequest
from backend.scrapers.auto_ru import AutoRuScraper
from backend.scrapers.base import CaptchaRequiredError

QUERY = SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)
DETAIL_URL = "https://auto.ru/cars/used/sale/bmw/5er/1134567890-deadbeef/"


def json_ld(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def search_html() -> bytes:
    payload = {
        "@context": "https://schema.org",
        "@type": "Product",
        "offers": {
            "@type": "AggregateOffer",
            "offerCount": 1,
            "offers": [{"@type": "Offer", "url": DETAIL_URL, "price": 3_990_000}],
        },
    }
    return (
        '<html><script type="application/ld+json">' + json_ld(payload) + "</script></html>"
    ).encode()


def detail_html() -> bytes:
    payload = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "BMW 5 серии 520i VII (G30/G31/G38) Рестайлинг",
        "brand": "BMW",
        "productionDate": "2022",
        "offers": {
            "@type": "Offer",
            "url": DETAIL_URL,
            "price": 3_990_000,
            "priceCurrency": "RUR",
            "availability": "https://schema.org/InStock",
        },
    }
    html = f"""
    <html><head>
      <title>Купить BMW 520i седан 2022 года в Москве: автомобиль на Авто.ру</title>
      <meta name="description" content="BMW 520i 2.0 AT, синий седан 2022 года в Москве с пробегом">
      <script type="application/ld+json">{json_ld(payload)}</script>
    </head></html>
    """
    return html.encode()


def test_builds_verified_bmw_search_url() -> None:
    scraper = AutoRuScraper(settings=Settings(_env_file=None))
    assert scraper.build_search_url(QUERY).endswith(
        "/cars/bmw/5er-520/used/?year_from=2022&year_to=2022&price_from=3280000&price_to=4920000"
    )


def test_parses_detail_json_ld_and_stable_metadata() -> None:
    listing = AutoRuScraper.parse_detail(detail_html(), DETAIL_URL, QUERY)
    assert listing is not None
    assert listing.external_id == "1134567890"
    assert listing.model == "520i"
    assert listing.body_type == "sedan"
    assert listing.price == 3_990_000
    assert listing.location == "Москва"
    assert listing.city == "Москва"
    assert listing.transmission == "automatic"


def test_search_uses_structured_data_without_css_selectors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        content = detail_html() if str(request.url).startswith(DETAIL_URL) else search_html()
        return httpx.Response(200, content=content, request=request)

    scraper = AutoRuScraper(
        settings=Settings(_env_file=None), transport=httpx.MockTransport(handler)
    )
    listings = asyncio.run(scraper.search(QUERY))
    assert len(listings) == 1
    assert listings[0].source == "auto.ru"


def test_captcha_is_reported_explicitly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="Подтвердите, что вы не робот CAPTCHA",
            request=request,
        )

    scraper = AutoRuScraper(
        settings=Settings(_env_file=None), transport=httpx.MockTransport(handler)
    )
    try:
        asyncio.run(scraper.search(QUERY))
    except CaptchaRequiredError:
        pass
    else:
        raise AssertionError("CAPTCHA response must not be treated as an empty result")


def test_broad_search_url_has_only_market_constraints() -> None:
    scraper = AutoRuScraper(settings=Settings(_env_file=None))
    discovery = MarketDiscoveryRequest(
        source_vehicle=QUERY,
        price_from=3_280_000,
        price_to=4_920_000,
        body_types={"sedan", "liftback"},
    )
    url = scraper.build_discovery_url(discovery, QUERY.body_type)
    assert url.endswith("/cars/used/body-sedan/?price_from=3280000&price_to=4920000")
    assert "bmw" not in url and "520" not in url


def test_auto_ru_region_and_transmission_are_part_of_search_url() -> None:
    query = SearchRequest(
        brand="Renault",
        model="Captur",
        year=2017,
        body_type="crossover",
        transmission="automatic",
        region="moscow_and_oblast",
        price=1_000_000,
    )
    url = AutoRuScraper(settings=Settings(_env_file=None)).build_search_url(query)
    assert "/moskovskaya_oblast/cars/renault/kaptur/used/" in url
    assert "transmission=AUTOMATIC" in url


def test_discovery_detail_keeps_family_and_modification() -> None:
    listing = AutoRuScraper.parse_discovery_detail(detail_html(), DETAIL_URL)
    assert listing.model == "5 Series"
    assert listing.modification is not None
    assert "520i" in listing.modification
