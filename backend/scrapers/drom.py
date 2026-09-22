import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import ValidationError

from backend.config import Settings, get_settings
from backend.models.car import BodyFilter, BodyType, MarketDiscoveryRequest, SearchRequest
from backend.models.listing import CarListing
from backend.scrapers.base import (
    AuthenticationRequiredError,
    BaseScraper,
    BrowserAccessLimitedError,
    CaptchaRequiredError,
    Http403Error,
    HttpAutomationLimitedError,
    ScraperParseError,
)
from backend.services.marketplace_catalog import CatalogCache
from backend.services.normalizer import Normalizer
from backend.services.regions import drom_region_prefix, drom_region_query

_DROM_ID = re.compile(r"/(\d+)\.html$")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_DROM_BODY_PATHS: dict[BodyType, str] = {
    BodyType.SEDAN: "sedan",
    BodyType.WAGON: "wagon",
    BodyType.HATCHBACK: "hatchback",
    BodyType.LIFTBACK: "liftback",
    BodyType.COUPE: "coupe",
    BodyType.CONVERTIBLE: "cabriolet",
    BodyType.SUV: "suv",
    BodyType.CROSSOVER: "suv",
    BodyType.PICKUP: "pickup",
    BodyType.MINIVAN: "minivan",
    BodyType.VAN: "van",
}


class DromScraper(BaseScraper):
    source = "drom.ru"

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        catalog: CatalogCache | None = None,
        browser_loader: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        self.catalog = catalog or CatalogCache()
        self.browser_loader = browser_loader or self._load_in_isolated_browser

    async def search(self, query: SearchRequest) -> list[CarListing]:
        url = self.build_search_url(query)
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
            timeout=self.settings.scraper_timeout_seconds,
            transport=self.transport,
        ) as client:
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                raise ScraperParseError("Drom request failed") from exc
        content = await self._content_with_fallback(response, url)
        return self.parse_search(content, query)

    async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
        listings: list[CarListing] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
            timeout=self.settings.scraper_timeout_seconds,
            transport=self.transport,
        ) as client:
            for body_type in sorted(query.body_types, key=str):
                try:
                    url = self.build_discovery_url(query, body_type)
                    response = await client.get(url)
                except httpx.HTTPError as exc:
                    raise ScraperParseError("Drom broad request failed") from exc
                content = await self._content_with_fallback(response, url)
                for listing in self.parse_discovery(content, body_type):
                    if listing.external_id in seen:
                        continue
                    if query.price_from <= listing.price <= query.price_to:
                        seen.add(listing.external_id)
                        listings.append(listing)
        return listings

    @staticmethod
    def build_discovery_url(query: MarketDiscoveryRequest, body_type: BodyType) -> str:
        path = _DROM_BODY_PATHS[body_type]
        region = drom_region_prefix(query.source_vehicle.region)
        region_query = drom_region_query(query.source_vehicle.region)
        return (
            f"https://auto.drom.ru/{region}{path}/?minprice={query.price_from}"
            f"&maxprice={query.price_to}&unsold=1{region_query}"
        )

    def build_search_url(self, query: SearchRequest) -> str:
        identity = Normalizer.vehicle_identity(query.brand, query.model)
        ref = self.catalog.source_model_ref("drom.ru", identity.brand, identity.family_model)
        if ref is None:
            raise ScraperParseError(
                f"Drom catalog has no model path for {query.brand} {query.model}"
            )
        parts = [part for part in ref.path.split("/") if part]
        if len(parts) < 3:
            raise ScraperParseError(f"Invalid Drom catalog path: {ref.path}")
        path = "/".join(parts[-2:])
        region = drom_region_prefix(query.region)
        region_query = drom_region_query(query.region)
        body_path = "" if query.body_type == BodyFilter.ANY else f"{query.body_type.value}/"
        return (
            f"https://auto.drom.ru/{region}{path}/year-{query.year}/used/{body_path}"
            f"?unsold=1&minprice={round(query.price * 0.8)}"
            f"&maxprice={round(query.price * 1.2)}{region_query}"
        )

    @classmethod
    def parse_search(cls, content: bytes, query: SearchRequest) -> list[CarListing]:
        soup = BeautifulSoup(content, "lxml")
        listings: list[CarListing] = []
        seen: set[str] = set()
        for card in soup.find_all(attrs={"data-ftid": "bulls-list_bull"}):
            if not isinstance(card, Tag):
                continue
            if card.find_parent(attrs={"data-ftid": "bulletin-list_archive"}) is not None:
                continue
            title = cls._text(card, "bull_title")
            subtitle = cls._text(card, "bull_subtitle")
            if not cls._model_matches(f"{title} {subtitle}", query.modification or query.model):
                continue
            link = card.find("a", attrs={"data-ftid": "bull_title"}, href=True)
            if not isinstance(link, Tag):
                continue
            url = str(link.get("href"))
            external_id = cls._external_id(url)
            if external_id in seen:
                continue
            year_match = _YEAR.search(title)
            if not year_match or int(year_match.group()) != query.year:
                continue
            price = Normalizer.price(cls._text(card, "bull_price"))
            location = cls._text(card, "bull_location") or None
            technical = cls._technical_metadata(subtitle)
            if query.body_type == BodyFilter.ANY:
                body_type = Normalizer.body_type_from_text(f"{title} {subtitle}")
                if body_type is None:
                    continue
            else:
                body_type = BodyType(query.body_type.value)
            try:
                listing = CarListing(
                    source=cls.source,
                    external_id=external_id,
                    brand=Normalizer.brand(query.brand),
                    model=Normalizer.model(query.model),
                    modification=subtitle or None,
                    fuel_type=technical["fuel_type"],
                    engine_displacement=technical["engine_displacement"],
                    power_hp=technical["power_hp"],
                    drivetrain=technical["drivetrain"],
                    year=query.year,
                    body_type=body_type,
                    transmission=Normalizer.transmission_from_text(subtitle),
                    price=price,
                    url=url,
                    location=location,
                    city=location,
                    checked_at=datetime.now(UTC),
                    raw_metadata={"title": title, "subtitle": subtitle},
                )
            except ValidationError as exc:
                raise ScraperParseError(f"Invalid Drom listing: {url}") from exc
            seen.add(external_id)
            listings.append(listing)
        return listings

    @classmethod
    def parse_discovery(cls, content: bytes, body_type: BodyType) -> list[CarListing]:
        soup = BeautifulSoup(content, "lxml")
        listings: list[CarListing] = []
        seen: set[str] = set()
        for card in soup.find_all(attrs={"data-ftid": "bulls-list_bull"}):
            if not isinstance(card, Tag):
                continue
            if card.find_parent(attrs={"data-ftid": "bulletin-list_archive"}) is not None:
                continue
            link = card.find("a", attrs={"data-ftid": "bull_title"}, href=True)
            if not isinstance(link, Tag):
                continue
            url = str(link.get("href"))
            external_id = cls._external_id(url)
            if external_id in seen:
                continue
            title = cls._text(card, "bull_title")
            year_match = _YEAR.search(title)
            if not year_match:
                continue
            name = re.split(r",\s*(?:19|20)\d{2}\b", title, maxsplit=1)[0].strip()
            brand, model = cls._split_name(name)
            subtitle = cls._text(card, "bull_subtitle")
            technical = cls._technical_metadata(subtitle)
            identity = Normalizer.marketplace_identity(
                brand, name, cls._model_slug(url), subtitle or None
            )
            try:
                listing = CarListing(
                    source=cls.source,
                    external_id=external_id,
                    brand=identity.brand,
                    model=identity.family_model or model,
                    modification=identity.modification,
                    fuel_type=technical["fuel_type"],
                    engine_displacement=technical["engine_displacement"],
                    power_hp=technical["power_hp"],
                    drivetrain=technical["drivetrain"],
                    year=int(year_match.group()),
                    body_type=body_type,
                    transmission=Normalizer.transmission_from_text(subtitle),
                    price=Normalizer.price(cls._text(card, "bull_price")),
                    url=url,
                    location=cls._text(card, "bull_location") or None,
                    city=cls._text(card, "bull_location") or None,
                    checked_at=datetime.now(UTC),
                    raw_metadata={"title": title, "subtitle": subtitle},
                )
            except (ValidationError, ValueError) as exc:
                raise ScraperParseError(f"Invalid Drom listing: {url}") from exc
            seen.add(external_id)
            listings.append(listing)
        return listings

    @staticmethod
    def _split_name(name: str) -> tuple[str, str]:
        words = name.split()
        if len(words) < 2:
            raise ScraperParseError(f"Drom title has no make/model: {name}")
        if len(words) >= 3 and " ".join(words[:2]).casefold() in {
            "land rover",
            "alfa romeo",
            "great wall",
        }:
            return " ".join(words[:2]), " ".join(words[2:])
        return words[0], " ".join(words[1:])

    @staticmethod
    def _model_slug(url: str) -> str | None:
        parts = [part for part in urlparse(url).path.split("/") if part]
        return parts[-2] if len(parts) >= 2 else None

    @staticmethod
    def _detect_interruption(response: httpx.Response) -> None:
        soup = BeautifulSoup(response.content, "lxml")
        title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
        visible = soup.get_text(" ", strip=True).casefold()
        url = str(response.url).casefold()
        if response.status_code == 429:
            raise HttpAutomationLimitedError("Drom limited direct automated HTTP (429)")
        if "captcha" in url or "подтвердите, что вы не робот" in visible:
            raise CaptchaRequiredError("Drom requires a manual CAPTCHA check")
        if response.status_code == 403 or "доступ ограничен" in title:
            raise Http403Error("Drom access denied: HTTP 403")
        if response.status_code == 401:
            raise AuthenticationRequiredError("Drom authentication or access is required")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ScraperParseError(f"Drom returned HTTP {response.status_code}") from exc

    async def _content_with_fallback(self, response: httpx.Response, url: str) -> bytes:
        try:
            self._detect_interruption(response)
            return response.content
        except (HttpAutomationLimitedError, Http403Error):
            try:
                return await self.browser_loader(url)
            except CaptchaRequiredError:
                raise
            except BrowserAccessLimitedError:
                raise
            except Exception as exc:
                raise HttpAutomationLimitedError(
                    "Drom limited direct HTTP and isolated browser fallback failed"
                ) from exc

    async def _load_in_isolated_browser(self, url: str) -> bytes:
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
                await page.wait_for_timeout(1_500)
                title = (await page.title()).casefold()
                visible = (await page.locator("body").inner_text()).casefold()
                current_url = page.url.casefold()
                content = (await page.content()).encode()
                status = response.status if response else None
                await browser.close()
        except PlaywrightError as exc:
            raise HttpAutomationLimitedError("Drom isolated browser could not start") from exc
        if "captcha" in current_url or "подтвердите, что вы не робот" in visible:
            raise CaptchaRequiredError("Drom requires a manual CAPTCHA check")
        if status in {403, 429} or "доступ ограничен" in title:
            raise BrowserAccessLimitedError("Drom limited isolated browser access")
        if "bulls-list_bull" not in content.decode(errors="ignore"):
            raise BrowserAccessLimitedError("Drom browser page had no listing cards")
        return content

    @staticmethod
    def _text(card: Tag, marker: str) -> str:
        element = card.find(attrs={"data-ftid": marker})
        return element.get_text(" ", strip=True) if isinstance(element, Tag) else ""

    @staticmethod
    def _external_id(url: str) -> str:
        parsed = urlparse(url)
        if parsed.hostname != "auto.drom.ru":
            raise ScraperParseError(f"Unexpected Drom listing host: {url}")
        match = _DROM_ID.search(parsed.path)
        if not match:
            raise ScraperParseError(f"Drom listing id is missing: {url}")
        return match.group(1)

    @staticmethod
    def _model_matches(text: str, model: str) -> bool:
        compact_text = re.sub(r"[^a-zа-я0-9]", "", text.casefold())
        compact_model = re.sub(r"[^a-zа-я0-9]", "", model.casefold())
        return bool(compact_model) and compact_model in compact_text

    @staticmethod
    def _model_key(model: str) -> str:
        return re.sub(r"[^a-z0-9]", "", model.casefold())

    @staticmethod
    def _technical_metadata(text: str) -> dict[str, str | int | float | None]:
        folded = text.casefold()
        displacement = re.search(r"(\d+(?:[.,]\d+)?)\s*л\b", folded)
        power = re.search(r"(\d+)\s*л\.\s*с\.", folded)
        fuels = (
            ("диз", "дизель"),
            ("бенз", "бензин"),
            ("элект", "электро"),
            ("гибрид", "гибрид"),
        )
        fuel = next((value for token, value in fuels if token in folded), None)
        drivetrain = None
        if "4wd" in folded or "полный" in folded:
            drivetrain = "awd"
        elif "передн" in folded:
            drivetrain = "fwd"
        elif "задн" in folded:
            drivetrain = "rwd"
        return {
            "fuel_type": fuel,
            "engine_displacement": (
                float(displacement.group(1).replace(",", ".")) if displacement else None
            ),
            "power_hp": int(power.group(1)) if power else None,
            "drivetrain": drivetrain,
        }

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        if not slug:
            raise ValueError(f"Drom cannot build a slug from {value!r}")
        return slug
