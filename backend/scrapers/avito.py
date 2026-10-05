import asyncio
import json
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
from backend.models.car import (
    BodyFilter,
    BodyType,
    MarketDiscoveryRequest,
    RangeMode,
    SearchRegion,
    SearchRequest,
    Transmission,
)
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
from backend.services.regions import location_matches

_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_ID = re.compile(r"_(\d+)/?$")
_EMPTY_TEXT = ("ничего не найдено", "объявления не найдены", "нет объявлений")


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
        url = self.build_target_url(query)
        diagnostic = self.reset_diagnostics("target", url)
        listings = await self._collect(url, diagnostic, query=query)
        diagnostic.accepted_count = len(listings)
        return listings

    @staticmethod
    def _identity(card: Tag, title: str, query: SearchRequest | None) -> tuple[str, str]:
        brand_node = card.find(attrs={"itemprop": "brand"})
        model_node = card.find(attrs={"itemprop": "model"})
        if isinstance(brand_node, Tag) and isinstance(model_node, Tag):
            brand = str(brand_node.get("content") or brand_node.get_text(" ", strip=True))
            model = str(model_node.get("content") or model_node.get_text(" ", strip=True))
            if brand.strip() and model.strip():
                return brand, model

        for script in card.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            brand = data.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            model = data.get("model")
            if (
                isinstance(brand, str)
                and brand.strip()
                and isinstance(model, str)
                and model.strip()
            ):
                return brand, model

        if query is not None:
            # A target URL is only a search hint, not evidence of a card's identity.
            expected = f"{query.brand} {query.model}"
            if re.match(rf"^{re.escape(expected)}(?=$|[^\w])", title, re.I):
                return query.brand, query.model
        raise ValueError("missing verified brand/model")

    @classmethod
    def build_target_url(cls, query: SearchRequest) -> str:
        years = (
            str(query.effective_year_from)
            if query.effective_year_from == query.effective_year_to
            else f"{query.effective_year_from}-{query.effective_year_to}"
        )
        params = urlencode({"q": f"{query.brand} {query.model} {years}"})
        return f"https://www.avito.ru/{cls._region_path(query.region)}/avtomobili?{params}"

    async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
        url = self.build_discovery_url(query)
        diagnostic = self.reset_diagnostics("competitors", url)
        listings = await self._collect(url, diagnostic)
        accepted = []
        for item in listings:
            reason = self._discovery_rejection_reason(item, query)
            if reason:
                diagnostic.rejected[reason] = diagnostic.rejected.get(reason, 0) + 1
            else:
                accepted.append(item)
        diagnostic.accepted_count = len(accepted)
        return accepted

    @classmethod
    def build_discovery_url(cls, query: MarketDiscoveryRequest) -> str:
        params = urlencode({"pmin": query.price_from, "pmax": query.price_to})
        return (
            f"https://www.avito.ru/{cls._region_path(query.source_vehicle.region)}/"
            f"avtomobili?{params}"
        )

    async def _collect(
        self,
        url: str,
        diagnostic: SourceDiagnostics,
        query: SearchRequest | None = None,
    ) -> list[CarListing]:
        listings: list[CarListing] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        current_url: str | None = url
        page_limit = min(self.settings.live_search_max_pages, self.settings.scraper_max_pages)
        while current_url and len(seen_urls) < page_limit:
            if current_url in seen_urls:
                break
            seen_urls.add(current_url)
            try:
                content = await self._content(current_url, diagnostic)
            except (ScraperError, TimeoutError) as exc:
                if not listings:
                    raise
                diagnostic.degraded = True
                diagnostic.partial_failures += 1
                diagnostic.notes.append(f"Later Avito page failed: {type(exc).__name__}")
                break
            diagnostic.pages_scanned += 1
            diagnostic.resolved_urls.append(current_url)
            raw_before = diagnostic.raw_count
            parsed_before = diagnostic.parsed_count
            page = self.parse_listings(content, query=query, diagnostic=diagnostic)
            if diagnostic.raw_count > raw_before and diagnostic.parsed_count == parsed_before:
                if not listings:
                    raise ScraperParseError("Avito cards have no parseable listing data")
                diagnostic.degraded = True
                diagnostic.partial_failures += 1
                break
            new = [item for item in page if item.external_id not in seen_ids]
            if not new:
                break
            for item in new:
                seen_ids.add(item.external_id)
                if query is not None:
                    reason = self._target_rejection_reason(item, query)
                    if reason:
                        diagnostic.rejected[reason] = diagnostic.rejected.get(reason, 0) + 1
                        continue
                listings.append(item)
                if len(listings) >= self.settings.scraper_max_listings:
                    return listings
            current_url = self._next_page_url(content, current_url)
        return listings

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
        if self._has_listing_cards(response.content) or self._is_valid_empty(response.content):
            return response.content
        content = await self._browser_content(url, diagnostic)
        if not self._has_listing_cards(content) and not self._is_valid_empty(content):
            raise ScraperParseError("Avito browser page has no listing or empty-result content")
        return content

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
                parsed_url = urlparse(url)
                if parsed_url.netloc not in {"www.avito.ru", "avito.ru"}:
                    raise ValueError("listing link is not Avito")
                id_match = _ID.search(parsed_url.path)
                if id_match is None:
                    raise ValueError("missing listing id")
                external_id = id_match.group(1)
                if external_id in seen:
                    continue
                title = anchor.get_text(" ", strip=True) or str(anchor.get("title") or "")
                text = card.get_text(" ", strip=True)
                year_match = _YEAR.search(title)
                if year_match is None:
                    raise ValueError("missing year")
                price_node = card.find(attrs={"itemprop": "price"}) or card.find(
                    attrs={"data-marker": "item-price"}
                )
                if not isinstance(price_node, Tag):
                    raise ValueError("missing listing price")
                price_text = (
                    str(price_node.get("content"))
                    if price_node.get("content")
                    else price_node.get_text(" ", strip=True)
                )
                body = Normalizer.body_type_from_text(text)
                brand, model = cls._identity(card, title, query)
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
                    modification=None,
                    year=int(year_match.group()),
                    body_type=body,
                    transmission=Normalizer.transmission_from_text(text),
                    price=Normalizer.price(price_text),
                    url=url,
                    location=location,
                    city=None,
                    checked_at=datetime.now(UTC),
                    raw_metadata={"title": title},
                )
            except (ValidationError, ValueError):
                if diagnostic is not None:
                    diagnostic.rejected["parse"] = diagnostic.rejected.get("parse", 0) + 1
                continue
            seen.add(external_id)
            if diagnostic is not None:
                diagnostic.parsed_count += 1
            listings.append(listing)
        return listings

    @staticmethod
    def _target_rejection_reason(listing: CarListing, query: SearchRequest) -> str | None:
        if listing.brand.casefold() != Normalizer.brand(query.brand).casefold() or (
            listing.model.casefold() != query.model.casefold()
        ):
            return "model"
        if not query.effective_year_from <= listing.year <= query.effective_year_to:
            return "year"
        if query.price_mode == RangeMode.RANGE and not (
            query.effective_price_from <= listing.price <= query.effective_price_to
        ):
            return "price"
        if query.body_type != BodyFilter.ANY and (
            listing.body_type is None or listing.body_type.value != query.body_type.value
        ):
            return "body"
        if query.transmission != Transmission.ANY and listing.transmission != query.transmission:
            return "transmission"
        if not location_matches(query.region, listing.location, listing.city, listing.region):
            return "region"
        return None

    @staticmethod
    def _discovery_rejection_reason(
        listing: CarListing, query: MarketDiscoveryRequest
    ) -> str | None:
        if not query.price_from <= listing.price <= query.price_to:
            return "price"
        if len(query.body_types) != len(BodyType) and listing.body_type not in query.body_types:
            return "body"
        if not location_matches(
            query.source_vehicle.region, listing.location, listing.city, listing.region
        ):
            return "region"
        return None

    @staticmethod
    def _has_listing_cards(content: bytes) -> bool:
        return bool(BeautifulSoup(content, "lxml").find(attrs={"data-marker": "item"}))

    @staticmethod
    def _is_valid_empty(content: bytes) -> bool:
        soup = BeautifulSoup(content, "lxml")
        text = soup.get_text(" ", strip=True).casefold()
        return any(message in text for message in _EMPTY_TEXT)

    @staticmethod
    def _next_page_url(content: bytes, current_url: str) -> str | None:
        soup = BeautifulSoup(content, "lxml")
        anchor = soup.select_one(
            'a[rel="next"][href], a[data-marker="pagination-button/next"][href]'
        )
        if not isinstance(anchor, Tag):
            return None
        candidate = urljoin(current_url, str(anchor["href"]))
        current = urlparse(current_url)
        next_page = urlparse(candidate)
        if (
            next_page.scheme != "https"
            or next_page.netloc not in {"www.avito.ru", "avito.ru"}
            or next_page.path != current.path
            or candidate == current_url
        ):
            return None
        return candidate

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
