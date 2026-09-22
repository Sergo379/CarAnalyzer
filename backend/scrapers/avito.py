import asyncio
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from backend.config import Settings, get_settings
from backend.models.car import MarketDiscoveryRequest, SearchRegion, SearchRequest
from backend.models.listing import CarListing
from backend.scrapers.base import (
    AuthenticationRequiredError,
    BaseScraper,
    CaptchaRequiredError,
    Http403Error,
    Http429Error,
    ScraperError,
    ScraperParseError,
)


class AvitoScraper(BaseScraper):
    """Avito adapter with explicit protection-state reporting.

    The current development network is rejected by Avito with HTTP 429. Parsing
    selectors are intentionally not invented until a legitimate response can be
    inspected. This adapter still provides the production-safe source boundary.
    """

    source = "avito"

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        self._browser_lock = asyncio.Lock()
        self._browser_error: ScraperError | None = None

    async def search(self, query: SearchRequest) -> list[CarListing]:
        params = urlencode({"q": f"{query.brand} {query.model} {query.year}"})
        url = f"https://www.avito.ru/{self._region_path(query.region)}/avtomobili?{params}"
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
            timeout=self.settings.scraper_timeout_seconds,
            transport=self.transport,
        ) as client:
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                raise ScraperParseError("Avito request failed") from exc

        self._detect_interruption(response)
        await self._confirm_rendered_access(url)
        raise ScraperParseError("Avito rendered page has no verified listing cards")

    async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
        # Only the category route has been observed on this network. Filter parameter
        # names and listing selectors are intentionally not guessed while Avito blocks it.
        url = f"https://www.avito.ru/{self._region_path(query.source_vehicle.region)}/avtomobili"
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
            timeout=self.settings.scraper_timeout_seconds,
            transport=self.transport,
        ) as client:
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                raise ScraperParseError("Avito broad request failed") from exc
        self._detect_interruption(response)
        await self._confirm_rendered_access(url)
        raise ScraperParseError("Avito rendered broad page has no verified listing cards")

    @staticmethod
    def _region_path(region: SearchRegion) -> str:
        if region == SearchRegion.MOSCOW:
            return "moskva"
        if region == SearchRegion.MOSCOW_OBLAST:
            return "moskovskaya_oblast"
        if region == SearchRegion.MOSCOW_AND_OBLAST:
            return "moskva_i_mo"
        return "rossiya"

    async def _confirm_rendered_access(self, url: str) -> None:
        """Use an isolated browser only to distinguish shell/block/CAPTCHA states."""
        async with self._browser_lock:
            if self._browser_error is not None:
                raise self._browser_error
            try:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=True)
                    context = await browser.new_context(locale="ru-RU")
                    page = await context.new_page()
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(self.settings.scraper_timeout_seconds * 1000),
                    )
                    await page.wait_for_timeout(2_000)
                    title = (await page.title()).casefold()
                    current_url = page.url.casefold()
                    visible = (await page.locator("body").inner_text()).casefold()
                    status = response.status if response else None
                    await browser.close()
            except PlaywrightError as exc:
                error = ScraperParseError("Avito isolated browser failed")
                self._browser_error = error
                raise error from exc
            if status == 429 or "проблема с ip" in title:
                self._browser_error = Http429Error(
                    "Avito limited isolated browser access with HTTP 429"
                )
            elif status == 403:
                self._browser_error = Http403Error(
                    "Avito limited isolated browser access with HTTP 403"
                )
            elif "captcha" in current_url or "проверка, что вы не робот" in visible:
                self._browser_error = CaptchaRequiredError("Avito requires manual CAPTCHA")
            if self._browser_error is not None:
                raise self._browser_error

    @staticmethod
    def _detect_interruption(response: httpx.Response) -> None:
        soup = BeautifulSoup(response.content, "lxml")
        title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
        visible_text = soup.get_text(" ", strip=True).casefold()
        final_url = str(response.url).casefold()
        if response.status_code == 429 or "доступ ограничен" in title:
            raise Http429Error("Avito limited automated HTTP access: HTTP 429")
        if "captcha" in final_url or "проверка, что вы не робот" in visible_text:
            raise CaptchaRequiredError("Avito requires a manual CAPTCHA check")
        if response.status_code == 403:
            raise Http403Error("Avito access denied: HTTP 403")
        if response.status_code == 401 or "/login" in final_url:
            raise AuthenticationRequiredError("Avito authentication is required")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ScraperParseError(f"Avito returned HTTP {response.status_code}") from exc
