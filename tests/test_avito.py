import asyncio

import httpx
import pytest

from backend.config import Settings
from backend.models.car import SearchRequest
from backend.scrapers.avito import AvitoScraper
from backend.scrapers.base import Http429Error


def test_avito_reports_ip_block_instead_of_empty_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            text="<title>Доступ ограничен: проблема с IP</title>",
            request=request,
        )

    scraper = AvitoScraper(
        settings=Settings(_env_file=None), transport=httpx.MockTransport(handler)
    )
    query = SearchRequest(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)
    with pytest.raises(Http429Error):
        asyncio.run(scraper.search(query))
