import asyncio

import httpx
import pytest

from backend.config import Settings
from backend.models.car import MarketDiscoveryRequest, SearchRequest
from backend.scrapers.avito import AvitoScraper
from backend.scrapers.base import BrowserAccessLimitedError


def test_avito_http_limit_transitions_to_isolated_browser() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
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
    with pytest.raises(BrowserAccessLimitedError):
        asyncio.run(scraper.search(query))
    assert len(calls) == 1


def test_avito_browser_probe_is_shared_between_target_and_discovery() -> None:
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
        assert all(isinstance(value, BrowserAccessLimitedError) for value in results)

    asyncio.run(run())
    assert len(calls) == 1
