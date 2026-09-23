import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import ValidationError

from backend.config import Settings, get_settings
from backend.models.car import MarketDiscoveryRequest, SearchRegion, SearchRequest
from backend.models.listing import CarListing, SourceDiagnostics
from backend.scrapers.base import (
    AuthenticationRequiredError,
    BaseScraper,
    BrowserAccessLimitedError,
    CaptchaRequiredError,
    Http403Error,
    Http429Error,
    ScraperError,
    ScraperParseError,
)
from backend.services.normalizer import Normalizer

_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_ID = re.compile(r"_(\d+)(?:\?.*)?$")


class AvitoScraper(BaseScraper):
    """Read-only Avito adapter with semantic parsing and an isolated profile fallback."""

    source = "avito"

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        browser_checker: Callable[[str], Awaitable[bytes | None]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        self.browser_checker = browser_checker or self._load_rendered_content
        self._browser_lock = asyncio.Lock()
        self._browser_error: Exception | None = None
        self._browser_pages: dict[str, bytes] = {}

    async def search(self, query: SearchRequest) -> list[CarListing]:
        years = (
            str(query.effective_year_from)
            if query.effective_year_from == query.effective_year_to
            else f"{query.effective_year_from}-{query.effective_year_to}"
        )
        params = urlencode({"q": f"{query.brand} {query.model} {years}"})
        url = f"https://www.avito.ru/{self._region_path(query.region)}/avtomobili?{params}"
        diagnostic = self.reset_diagnostics("target", url)
        content = await self._content(url, diagnostic)
        listings = self.parse_listings(content, query=query, diagnostic=diagnostic)
        diagnostic.accepted_count = len(listings)
        if not listings:
            raise ScraperParseError("Avito page is accessible but has no recognized target cards")
        return listings

    async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
        params = urlencode({"pmin": query.price_from, "pmax": query.price_to})
        url = (
            f"https://www.avito.ru/{self._region_path(query.source_vehicle.region)}/"
            f"avtomobili?{params}"
        )
        diagnostic = self.reset_diagnostics("competitors", url)
        content = await self._content(url, diagnostic)
        listings = self.parse_listings(content, diagnostic=diagnostic)
        accepted = [
            item
            for item in listings
            if query.price_from <= item.price <= query.price_to
            and item.body_type in query.body_types
        ]
        diagnostic.accepted_count = len(accepted)
        if not listings:
            raise ScraperParseError("Avito page is accessible but has no recognized broad cards")
        return accepted

    async def _content(self, url: str, diagnostic: SourceDiagnostics) -> bytes:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
            timeout=self.settings.scraper_timeout_seconds,
            transport=self.transport,
        ) as client:
            try:
                diagnostic.http_requests += 1
                response = await client.get(url)
            except httpx.TimeoutException as exc:
                raise TimeoutError("Avito HTTP request timed out") from exc
            except httpx.HTTPError as exc:
                raise ScraperError("Avito HTTP request failed") from exc
        self._detect_interruption(response)
        if self._has_listing_cards(response.content):
            return response.content
        return await self._browser_content(url, diagnostic)

    async def _browser_content(self, url: str, diagnostic: SourceDiagnostics) -> bytes:
        async with self._browser_lock:
            if self._browser_error is not None:
                raise self._browser_error
            if url in self._browser_pages:
                return self._browser_pages[url]
            try:
                diagnostic.browser_fallbacks += 1
                content = await asyncio.wait_for(
                    self.browser_checker(url),
                    timeout=self.settings.avito_browser_timeout_seconds,
                )
            except TimeoutError as exc:
                self._browser_error = TimeoutError("Avito browser fallback timed out")
                raise self._browser_error from exc
            except ScraperError as exc:
                self._browser_error = exc
                raise
            if not content:
                raise ScraperParseError("Avito isolated browser returned no page content")
            self._browser_pages[url] = content
            return content

    @classmethod
    def parse_listings(
        cls,
        content: bytes,
        query: SearchRequest | None = None,
        diagnostic: SourceDiagnostics | None = None,
    ) -> list[CarListing]:
        soup = BeautifulSoup(content, "lxml")
        cards = soup.find_all(attrs={"data-marker": "item"})
        listings: list[CarListing] = []
        seen: set[str] = set()
        if diagnostic is not None:
            diagnostic.raw_count += len(cards)
        for card in cards:
            try:
                if not isinstance(card, Tag):
                    raise ValueError("invalid card")
                anchor = card.find("a", attrs={"data-marker": "item-title"}, href=True)
                if not isinstance(anchor, Tag):
                    anchor = card.find("a", href=re.compile(r"/avtomobili/"))
                if not isinstance(anchor, Tag):
                    raise ValueError("missing listing link")
                url = urljoin("https://www.avito.ru", str(anchor["href"]))
                id_match = _ID.search(urlparse(url).path)
                if id_match is None:
                    raise ValueError("missing listing id")
                external_id = id_match.group(1)
                if external_id in seen:
                    continue
                title = anchor.get_text(" ", strip=True)
                text = card.get_text(" ", strip=True)
                year_match = _YEAR.search(title)
                if year_match is None:
                    raise ValueError("missing year")
                price_node = card.find(attrs={"itemprop": "price"}) or card.find(
                    attrs={"data-marker": "item-price"}
                )
                price_text = (
                    str(price_node.get("content"))
                    if isinstance(price_node, Tag) and price_node.get("content")
                    else price_node.get_text(" ", strip=True)
                    if isinstance(price_node, Tag)
                    else text
                )
                body = Normalizer.body_type_from_text(text)
                if body is None:
                    raise ValueError("missing body type")
                if query is not None:
                    brand, model = query.brand, query.model
                else:
                    brand_node = card.find(attrs={"itemprop": "brand"})
                    model_node = card.find(attrs={"itemprop": "model"})
                    if not isinstance(brand_node, Tag) or not isinstance(model_node, Tag):
                        raise ValueError("missing structured brand/model")
                    brand = str(brand_node.get("content") or brand_node.get_text(" ", strip=True))
                    model = str(model_node.get("content") or model_node.get_text(" ", strip=True))
                location_node = card.find(attrs={"data-marker": "item-address"})
                location = (
                    location_node.get_text(" ", strip=True)
                    if isinstance(location_node, Tag)
                    else None
                )
                listing = CarListing(
                    source=cls.source,
                    external_id=external_id,
                    brand=Normalizer.brand(brand),
                    model=Normalizer.model(model),
                    modification=title,
                    year=int(year_match.group()),
                    body_type=body,
                    transmission=Normalizer.transmission_from_text(text),
                    price=Normalizer.price(price_text),
                    url=url,
                    location=location,
                    city=location,
                    checked_at=datetime.now(UTC),
                    raw_metadata={"title": title},
                )
                if query is not None and not cls._target_matches(listing, query):
                    continue
            except (ValidationError, ValueError):
                if diagnostic is not None:
                    diagnostic.rejected["parse"] = diagnostic.rejected.get("parse", 0) + 1
                continue
            seen.add(external_id)
            listings.append(listing)
        if diagnostic is not None:
            diagnostic.parsed_count += len(listings)
        return listings

    @staticmethod
    def _target_matches(listing: CarListing, query: SearchRequest) -> bool:
        return (
            query.effective_year_from <= listing.year <= query.effective_year_to
            and (query.body_type.value == "any" or listing.body_type.value == query.body_type.value)
            and (query.transmission.value == "any" or listing.transmission == query.transmission)
        )

    @staticmethod
    def _has_listing_cards(content: bytes) -> bool:
        return bool(BeautifulSoup(content, "lxml").find(attrs={"data-marker": "item"}))

    @staticmethod
    def _region_path(region: SearchRegion) -> str:
        return {
            SearchRegion.MOSCOW: "moskva",
            SearchRegion.MOSCOW_OBLAST: "moskovskaya_oblast",
            SearchRegion.MOSCOW_AND_OBLAST: "moskva_i_mo",
        }.get(region, "rossiya")

    async def _load_rendered_content(self, url: str) -> bytes:
        """Use only CarAnalyzer's dedicated profile; never a personal browser profile."""
        context = None
        page = None
        profile = self.settings.resolved_avito_browser_data_path()
        profile.mkdir(parents=True, exist_ok=True)
        try:
            async with async_playwright() as playwright:
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        str(profile), headless=False, locale="ru-RU"
                    )
                    page = context.pages[0] if context.pages else await context.new_page()
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(self.settings.scraper_timeout_seconds * 1000),
                    )
                    await page.wait_for_timeout(2_000)
                    title = (await page.title()).casefold()
                    current_url = page.url.casefold()
                    visible = (await page.locator("body").inner_text()).casefold()
                    content = (await page.content()).encode()
                    status = response.status if response else None
                finally:
                    await self._close_browser_resources(page, context)
        except PlaywrightError as exc:
            raise ScraperParseError("Avito isolated browser failed") from exc
        if status == 429 or "проблема с ip" in title:
            raise BrowserAccessLimitedError("Avito limited isolated browser access with HTTP 429")
        if status == 403:
            raise BrowserAccessLimitedError("Avito limited isolated browser access with HTTP 403")
        if "captcha" in current_url or "проверка, что вы не робот" in visible:
            raise CaptchaRequiredError(
                "Avito requires manual CAPTCHA in the dedicated CarAnalyzer profile"
            )
        return content

    @staticmethod
    async def _close_browser_resources(page, context) -> None:
        async def close() -> None:
            if page is not None:
                with suppress(PlaywrightError):
                    await page.close()
            if context is not None:
                with suppress(PlaywrightError):
                    await context.close()

        with suppress(TimeoutError, PlaywrightError):
            await asyncio.shield(asyncio.wait_for(close(), timeout=3))

    @staticmethod
    def _detect_interruption(response: httpx.Response) -> None:
        soup = BeautifulSoup(response.content, "lxml")
        title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
        visible_text = soup.get_text(" ", strip=True).casefold()
        final_url = str(response.url).casefold()
        if response.status_code == 429:
            raise Http429Error("Avito limited automated HTTP access: HTTP 429")
        if response.status_code == 403:
            raise Http403Error("Avito access denied: HTTP 403")
        if "captcha" in final_url or "проверка, что вы не робот" in visible_text:
            raise CaptchaRequiredError("Avito requires a manual CAPTCHA check")
        if "доступ ограничен" in title:
            raise Http429Error("Avito limited automated HTTP access")
        if response.status_code == 401 or "/login" in final_url:
            raise AuthenticationRequiredError("Avito authentication is required")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ScraperError(f"Avito returned HTTP {response.status_code}") from exc
