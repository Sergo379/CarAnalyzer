import asyncio

import httpx
import pytest

from backend.config import Settings
from backend.models.car import MarketDiscoveryRequest, SearchRequest
from backend.models.listing import SourceDiagnostics
from backend.scrapers.avito import AvitoScraper
from backend.scrapers.base import BrowserAccessLimitedError, Http403Error, Http429Error


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
    query = SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)
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
    query = SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)
    discovery = MarketDiscoveryRequest(
        source_vehicle=query, price_from=3_280_000, price_to=4_920_000, body_types={"sedan"}
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
