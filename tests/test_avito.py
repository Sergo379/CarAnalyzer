import asyncio

import httpx
import pytest

from backend.config import Settings
from backend.models.car import MarketDiscoveryRequest, SearchRequest
from backend.models.listing import SourceDiagnostics
from backend.scrapers.avito import AvitoScraper
from backend.scrapers.base import (
    BrowserAccessLimitedError,
    CaptchaRequiredError,
    Http403Error,
    Http429Error,
    ScraperParseError,
)


def _query(**changes) -> SearchRequest:
    return SearchRequest(
        brand="Example Make", model="Model A", year=2020, price=2_000_000, **changes
    )


def _card(
    external_id: int,
    *,
    title: str = "Example Make Model A, 2020",
    price: int = 2_000_000,
    details: str = "седан автомат",
    location: str = "Москва",
    structured: bool = False,
) -> str:
    identity = (
        '<meta itemprop="brand" content="Example Make">'
        '<meta itemprop="model" content="Model A">'
        if structured
        else ""
    )
    return (
        f'<div data-marker="item"><a data-marker="item-title" '
        f'href="/moskva/avtomobili/example_make_model_a_2020_{external_id}">{title}</a>'
        f'<meta itemprop="price" content="{price}">{identity}'
        f'<span data-marker="item-address">{location}</span><span>{details}</span></div>'
    )


def _page(*cards: str, next_href: str | None = None) -> str:
    next_link = f'<a rel="next" href="{next_href}">Следующая</a>' if next_href else ""
    return f"<html><body>{''.join(cards)}{next_link}</body></html>"


@pytest.mark.parametrize(("status", "error"), [(429, Http429Error), (403, Http403Error)])
def test_avito_http_limit_does_not_invoke_browser(
    status: int, error: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            text="<title>Доступ ограничен: проблема с IP</title>",
            request=request,
        )

    calls: list[str] = []

    async def browser_checker(url: str) -> None:
        calls.append(url)
        raise BrowserAccessLimitedError("isolated browser limited")

    scraper = AvitoScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        browser_checker=browser_checker,
    )
    query = SearchRequest(
        brand="Example Make", model="Model A", year=2020, body_type="sedan", price=2_000_000
    )
    with pytest.raises(error):
        asyncio.run(scraper.search(query))
    assert calls == []


def test_avito_http_429_is_independent_for_target_and_discovery() -> None:
    calls: list[str] = []

    async def browser_checker(url: str) -> None:
        calls.append(url)
        raise BrowserAccessLimitedError("isolated browser limited")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request)

    scraper = AvitoScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        browser_checker=browser_checker,
    )
    query = SearchRequest(
        brand="Example Make", model="Model A", year=2020, body_type="sedan", price=2_000_000
    )
    discovery = MarketDiscoveryRequest(
        source_vehicle=query, price_from=1_600_000, price_to=2_400_000, body_types={"sedan"}
    )

    async def run() -> None:
        results = await asyncio.gather(
            scraper.search(query), scraper.discover(discovery), return_exceptions=True
        )
        assert all(isinstance(value, Http429Error) for value in results)

    asyncio.run(run())
    assert calls == []


def test_avito_browser_fallback_has_its_own_bounded_timeout() -> None:
    calls = 0

    async def slow_browser(url: str) -> bytes:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        return b""

    scraper = AvitoScraper(
        settings=Settings(_env_file=None, avito_browser_timeout_seconds=0.01),
        browser_checker=slow_browser,
    )

    async def run() -> None:
        for _ in range(2):
            with pytest.raises(TimeoutError, match="fallback timed out"):
                await scraper._browser_content("https://www.avito.ru/", SourceDiagnostics())

    asyncio.run(run())
    assert calls == 1


def test_avito_public_search_urls_use_selected_region_and_bounded_parameters() -> None:
    query = _query(region="moscow", body_type="sedan")
    discovery = MarketDiscoveryRequest(
        source_vehicle=query,
        price_from=1_500_000,
        price_to=2_500_000,
        body_types={"sedan"},
    )
    assert AvitoScraper.build_target_url(query) == (
        "https://www.avito.ru/moskva/avtomobili?q=Example+Make+Model+A+2020"
    )
    assert AvitoScraper.build_discovery_url(discovery) == (
        "https://www.avito.ru/moskva/avtomobili?pmin=1500000&pmax=2500000"
    )


def test_avito_direct_target_parses_verified_cards_without_browser() -> None:
    browser_calls = []

    async def browser_checker(url: str) -> bytes:
        browser_calls.append(url)
        return b""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_page(
                _card(101),
                _card(102, title="Other Make Model B, 2020"),
                _card(103, details="Без подробностей"),
            ),
            request=request,
        )

    scraper = AvitoScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        browser_checker=browser_checker,
    )
    listings = asyncio.run(scraper.search(_query()))
    assert [item.external_id for item in listings] == ["101", "103"]
    assert listings[0].source == "avito"
    assert listings[0].url.path.endswith("_101")
    assert listings[0].year == 2020
    assert listings[0].price == 2_000_000
    assert listings[0].location == "Москва"
    assert listings[0].city is None
    assert listings[0].modification is None
    assert listings[1].body_type is None
    assert listings[1].transmission is None
    assert scraper.diagnostics["target"].raw_count == 3
    assert scraper.diagnostics["target"].parsed_count == 2
    assert scraper.diagnostics["target"].accepted_count == 2
    assert scraper.diagnostics["target"].rejected == {"parse": 1}
    assert browser_calls == []


def test_avito_target_filters_year_price_range_and_region() -> None:
    query = SearchRequest(
        brand="Example Make", model="Model A", year=2020,
        price_mode="range", price_from=1_500_000, price_to=2_500_000,
        region="moscow", body_type="sedan", transmission="automatic",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_page(
                _card(101),
                _card(102, title="Example Make Model A, 2019"),
                _card(103, price=2_600_000),
                _card(104, location="Казань"),
                _card(105, details="седан механика"),
                _card(106, details="хэтчбек автомат"),
            ),
            request=request,
        )

    scraper = AvitoScraper(
        settings=Settings(_env_file=None), transport=httpx.MockTransport(handler)
    )
    listings = asyncio.run(scraper.search(query))
    assert [item.external_id for item in listings] == ["101"]
    assert scraper.diagnostics["target"].parsed_count == 6
    assert scraper.diagnostics["target"].rejected == {
        "year": 1, "price": 1, "region": 1, "transmission": 1, "body": 1,
    }


def test_avito_discovery_filters_price_body_region_without_guessing_identity() -> None:
    query = _query(region="moscow")
    discovery = MarketDiscoveryRequest(
        source_vehicle=query, price_from=1_500_000,
        price_to=2_500_000, body_types={"sedan"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_page(
                _card(101, structured=True),
                _card(102, price=3_000_000, structured=True),
                _card(103, details="хэтчбек", structured=True),
                _card(104, location="Казань", structured=True),
                _card(105, title="Unknown Vehicle, 2020"),
            ),
            request=request,
        )

    scraper = AvitoScraper(
        settings=Settings(_env_file=None), transport=httpx.MockTransport(handler)
    )
    listings = asyncio.run(scraper.discover(discovery))
    assert [item.external_id for item in listings] == ["101"]
    assert listings[0].brand == "Example make"
    assert listings[0].model == "Model A"
    assert scraper.diagnostics["competitors"].raw_count == 5
    assert scraper.diagnostics["competitors"].parsed_count == 4
    assert scraper.diagnostics["competitors"].accepted_count == 1
    assert scraper.diagnostics["competitors"].rejected == {
        "parse": 1, "price": 1, "body": 1, "region": 1,
    }


def test_avito_discovery_can_read_card_structured_json_identity() -> None:
    card = _card(101, title="Vehicle, 2020").replace(
        "</div>",
        '<script type="application/ld+json">'
        '{"brand":{"name":"Example Make"},"model":"Model A"}'
        "</script></div>",
    )
    listing = AvitoScraper.parse_listings(_page(card).encode())
    assert len(listing) == 1
    assert listing[0].brand == "Example make"
    assert listing[0].model == "Model A"


def test_avito_valid_empty_page_does_not_start_browser() -> None:
    browser_calls = []

    async def browser_checker(url: str) -> bytes:
        browser_calls.append(url)
        return b""

    scraper = AvitoScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, text="<html><body>Объявления не найдены</body></html>", request=request
            )
        ),
        browser_checker=browser_checker,
    )
    assert asyncio.run(scraper.search(_query())) == []
    assert scraper.diagnostics["target"].pages_scanned == 1
    assert browser_calls == []


def test_avito_js_shell_uses_bounded_browser_but_malformed_page_is_rejected() -> None:
    calls = []

    async def browser_checker(url: str) -> bytes:
        calls.append(url)
        return _page(_card(101)).encode()

    shell = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text="<html><div id='root'></div></html>", request=request
        )
    )
    scraper = AvitoScraper(
        settings=Settings(_env_file=None), transport=shell, browser_checker=browser_checker
    )
    assert len(asyncio.run(scraper.search(_query()))) == 1
    assert scraper.diagnostics["target"].browser_fallbacks == 1
    assert len(calls) == 1

    async def bad_browser(url: str) -> bytes:
        return b"<html><body>unrelated page</body></html>"

    scraper = AvitoScraper(
        settings=Settings(_env_file=None), transport=shell, browser_checker=bad_browser
    )
    with pytest.raises(ScraperParseError, match="no listing or empty-result"):
        asyncio.run(scraper.search(_query()))


def test_avito_captcha_is_reported_without_browser_fallback() -> None:
    calls = []

    async def browser_checker(url: str) -> bytes:
        calls.append(url)
        return b""

    scraper = AvitoScraper(
        settings=Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, text="<html><body>Проверка, что вы не робот</body></html>",
                request=request,
            )
        ),
        browser_checker=browser_checker,
    )
    with pytest.raises(CaptchaRequiredError):
        asyncio.run(scraper.search(_query()))
    assert calls == []


def test_avito_pagination_stops_when_ids_repeat_and_keeps_first_page_on_restriction() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "p=2" in str(request.url):
            return httpx.Response(200, text=_page(_card(101), next_href="?p=3"), request=request)
        return httpx.Response(200, text=_page(_card(101), next_href="?p=2"), request=request)

    scraper = AvitoScraper(
        settings=Settings(_env_file=None, live_search_max_pages=3),
        transport=httpx.MockTransport(handler),
    )
    assert [item.external_id for item in asyncio.run(scraper.search(_query()))] == ["101"]
    assert len(calls) == 2
    assert scraper.diagnostics["target"].pages_scanned == 2

    def restricted_handler(request: httpx.Request) -> httpx.Response:
        if "p=2" in str(request.url):
            return httpx.Response(429, request=request)
        return httpx.Response(200, text=_page(_card(101), next_href="?p=2"), request=request)

    scraper = AvitoScraper(
        settings=Settings(_env_file=None, live_search_max_pages=2),
        transport=httpx.MockTransport(restricted_handler),
    )
    assert [item.external_id for item in asyncio.run(scraper.search(_query()))] == ["101"]
    assert scraper.diagnostics["target"].degraded is True
    assert scraper.diagnostics["target"].partial_failures == 1
