import asyncio
import json

import httpx

from backend.config import Settings
from backend.models.car import MarketDiscoveryRequest, SearchRequest
from backend.models.catalog import CanonicalVehicleIdentity, SourceReference
from backend.scrapers.auto_ru import AutoRuScraper
from backend.scrapers.base import CaptchaRequiredError

QUERY = SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)
DETAIL_URL = "https://auto.ru/cars/used/sale/bmw/5er/1134567890-deadbeef/"


class FakeCatalog:
    def resolve_identity(self, brand: str, model: str) -> CanonicalVehicleIdentity:
        canonical_model = "5-Series" if brand.casefold() == "bmw" else model
        return CanonicalVehicleIdentity(
            canonical_brand_id=brand.casefold(),
            canonical_model_id=f"{brand.casefold()}:{canonical_model.casefold()}",
            brand=brand,
            model=canonical_model,
        )

    def source_model_ref(self, source: str, brand: str, model: str) -> SourceReference:
        slug = "5er" if brand.casefold() == "bmw" else "kaptur"
        return SourceReference(
            source=source,
            url=f"https://auto.ru/catalog/cars/{brand.casefold()}/{slug}/",
            path=f"/catalog/cars/{brand.casefold()}/{slug}/",
            slug=slug,
        )


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


def search_card_html() -> bytes:
    payload = {
        "@context": "https://schema.org",
        "@type": "Product",
        "offers": {
            "@type": "AggregateOffer",
            "offers": [{"url": DETAIL_URL, "price": 3_990_000}],
        },
    }
    return f'''<html><script type="application/ld+json">{json_ld(payload)}</script>
    <div data-seo="listing-item"><a href="{DETAIL_URL}">BMW 5 серии 520i, 2022</a>
    <div>Седан Автомат</div>
    <div class="ListingItemUniversalSeller__sellerAddress-x">Москва, Арбат</div></div>
    </html>'''.encode()


def test_builds_search_url_from_catalog_source_mapping() -> None:
    scraper = AutoRuScraper(settings=Settings(_env_file=None), catalog=FakeCatalog())  # type: ignore[arg-type]
    assert scraper.build_search_url(QUERY).endswith(
        "/cars/bmw/5er/used/?year_from=2022&year_to=2022&price_from=3280000&price_to=4920000"
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


def test_search_card_avoids_detail_request_when_required_fields_are_present() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=search_card_html(), request=request)

    scraper = AutoRuScraper(
        settings=Settings(_env_file=None), transport=httpx.MockTransport(handler)
    )
    listings = asyncio.run(scraper.search(QUERY))
    assert [item.external_id for item in listings] == ["1134567890"]
    assert calls == [scraper.build_search_url(QUERY)]
    assert scraper.combined_diagnostics().detail_requests == 0


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
    url = AutoRuScraper(
        settings=Settings(_env_file=None),
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    ).build_search_url(query)
    assert "/moskovskaya_oblast/cars/renault/kaptur/used/" in url
    assert "transmission=AUTOMATIC" in url


def test_discovery_detail_keeps_family_and_modification() -> None:
    listing = AutoRuScraper.parse_discovery_detail(detail_html(), DETAIL_URL)
    assert listing.model == "5er"
    assert listing.modification is not None
    assert "520i" in listing.modification


def test_auto_ru_follows_explicit_next_page() -> None:
    second_url = DETAIL_URL.replace("1134567890", "1134567891")

    def result_page(url: str, next_url: str | None = None) -> bytes:
        payload = {
            "@context": "https://schema.org",
            "@type": "Product",
            "offers": {"@type": "AggregateOffer", "offers": [{"url": url}]},
        }
        next_link = f'<link rel="next" href="{next_url}">' if next_url else ""
        return (
            f"<html><head>{next_link}</head>"
            f'<script type="application/ld+json">{json_ld(payload)}</script></html>'
        ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/sale/" in request.url.path:
            content = detail_html().replace(DETAIL_URL.encode(), url.encode())
        elif request.url.params.get("page") == "2":
            content = result_page(second_url)
        else:
            content = result_page(DETAIL_URL, "https://auto.ru/cars/bmw/used/?page=2")
        return httpx.Response(200, content=content, request=request)

    scraper = AutoRuScraper(
        settings=Settings(_env_file=None), transport=httpx.MockTransport(handler)
    )
    listings = asyncio.run(scraper.search(QUERY))
    assert {item.external_id for item in listings} == {"1134567890", "1134567891"}
    assert scraper.combined_diagnostics().pages_scanned == 2
