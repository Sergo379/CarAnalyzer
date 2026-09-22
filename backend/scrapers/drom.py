import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

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
        self._browser_lock = asyncio.Lock()
        self._browser_error: BrowserAccessLimitedError | None = None

    async def search(self, query: SearchRequest) -> list[CarListing]:
        target_queries = self._target_body_queries(query)
        target_by_url = {self.build_search_url(item): item for item in target_queries}
        first_url = next(iter(target_by_url))
        diagnostic = self.reset_diagnostics("target", first_url)
        diagnostic.resolved_urls = list(target_by_url)
        listings: list[CarListing] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
            timeout=self.settings.scraper_timeout_seconds,
            transport=self.transport,
        ) as client:
            for initial_url, target_query in target_by_url.items():
                url: str | None = initial_url
                for _ in range(self.settings.scraper_max_pages):
                    if url is None:
                        break
                    try:
                        response = await client.get(url)
                    except httpx.HTTPError as exc:
                        raise ScraperParseError("Drom request failed") from exc
                    content = await self._content_with_fallback(response, url, diagnostic)
                    diagnostic.pages_scanned += 1
                    diagnostic.raw_count += self._card_count(content)
                    parsed = self.parse_search(
                        content,
                        target_query,
                        body_resolver=lambda subtitle, year: self._catalog_body_type(
                            query, subtitle, year
                        ),
                    )
                    diagnostic.parsed_count += len(parsed)
                    new_count = 0
                    for listing in parsed:
                        if listing.external_id not in seen:
                            seen.add(listing.external_id)
                            listings.append(listing)
                            new_count += 1
                            if len(listings) >= self.settings.scraper_max_listings:
                                diagnostic.accepted_count = len(listings)
                                return listings
                    if new_count == 0:
                        break
                    url = self._next_page_url(content, str(response.url))
        diagnostic.accepted_count = len(listings)
        diagnostic.rejected["filters_or_parse"] = max(
            0, diagnostic.raw_count - diagnostic.accepted_count
        )
        return listings

    def _target_body_queries(self, query: SearchRequest) -> list[SearchRequest]:
        if query.body_type != BodyFilter.ANY or not hasattr(self.catalog, "generations"):
            return [query]
        bodies: set[BodyType] = set()
        for generation in self.catalog.generations(
            query.brand,
            query.model,
            year_from=query.effective_year_from,
            year_to=query.effective_year_to,
        ):
            bodies.update(BodyType(str(value)) for value in generation.get("body_types", []))
        if not bodies:
            return [query]
        return [
            query.model_copy(update={"body_type": BodyFilter(body.value)})
            for body in sorted(bodies, key=str)
        ]

    async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
        diagnostic = self.reset_diagnostics("competitors")
        listings: list[CarListing] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
            timeout=self.settings.scraper_timeout_seconds,
            transport=self.transport,
        ) as client:
            routes: dict[str, BodyType] = {}
            for body_type in sorted(query.body_types, key=str):
                initial_url = self.build_discovery_url(query, body_type)
                routes.setdefault(initial_url, self._route_body_type(body_type))
            diagnostic.resolved_urls = list(routes)
            diagnostic.resolved_url = next(iter(routes), None)

            async def collect_route(initial_url: str, route_body: BodyType) -> list[CarListing]:
                route_listings: list[CarListing] = []
                route_seen: set[str] = set()
                url: str | None = initial_url
                for _ in range(self.settings.scraper_max_pages):
                    if url is None:
                        break
                    try:
                        response = await client.get(url)
                    except httpx.HTTPError as exc:
                        raise ScraperParseError("Drom broad request failed") from exc
                    content = await self._content_with_fallback(response, url, diagnostic)
                    diagnostic.pages_scanned += 1
                    diagnostic.raw_count += self._card_count(content)
                    parsed = self.parse_discovery(content, route_body, diagnostic)
                    diagnostic.parsed_count += len(parsed)
                    new_count = 0
                    for listing in parsed:
                        if listing.external_id in route_seen:
                            continue
                        if query.price_from <= listing.price <= query.price_to:
                            route_seen.add(listing.external_id)
                            route_listings.append(listing)
                            new_count += 1
                            if len(route_listings) >= self.settings.scraper_max_listings:
                                return route_listings
                        else:
                            diagnostic.rejected["price"] = diagnostic.rejected.get("price", 0) + 1
                    if new_count == 0:
                        break
                    url = self._next_page_url(content, str(response.url))
                return route_listings

            batches = await asyncio.gather(
                *(collect_route(url, body) for url, body in routes.items())
            )
            for batch in batches:
                for listing in batch:
                    if listing.external_id in seen:
                        diagnostic.rejected["duplicate"] = (
                            diagnostic.rejected.get("duplicate", 0) + 1
                        )
                        continue
                    seen.add(listing.external_id)
                    listings.append(listing)
                    if len(listings) >= self.settings.scraper_max_listings:
                        diagnostic.accepted_count = len(listings)
                        return listings
        diagnostic.accepted_count = len(listings)
        return listings

    @staticmethod
    def _route_body_type(body_type: BodyType) -> BodyType:
        # Drom's /suv/ route combines crossovers and SUVs. Preserve the broad
        # source category instead of labelling every card as the requested subtype.
        return BodyType.SUV if _DROM_BODY_PATHS[body_type] == "suv" else body_type

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
        if hasattr(self.catalog, "resolve_identity"):
            identity = self.catalog.resolve_identity(query.brand, query.model)
            brand, model = identity.brand, identity.model
        else:
            normalized = Normalizer.vehicle_identity(query.brand, query.model)
            brand, model = normalized.brand, normalized.family_model
        ref = self.catalog.source_model_ref("drom.ru", brand, model)
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
        body_path = (
            ""
            if query.body_type == BodyFilter.ANY
            else f"{_DROM_BODY_PATHS[BodyType(query.body_type.value)]}/"
        )
        year_path = (
            f"year-{query.effective_year_from}"
            if query.effective_year_from == query.effective_year_to
            else f"year-{query.effective_year_from}-{query.effective_year_to}"
        )
        price_from = (
            query.effective_price_from
            if query.price_mode.value == "range"
            else round(query.reference_price * 0.8)
        )
        price_to = (
            query.effective_price_to
            if query.price_mode.value == "range"
            else round(query.reference_price * 1.2)
        )
        return (
            f"https://auto.drom.ru/{region}{path}/{year_path}/used/{body_path}"
            f"?unsold=1&minprice={price_from}&maxprice={price_to}{region_query}"
        )

    @classmethod
    def parse_search(
        cls,
        content: bytes,
        query: SearchRequest,
        body_resolver: Callable[[str, int], BodyType | None] | None = None,
    ) -> list[CarListing]:
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
            if not year_match:
                continue
            listing_year = int(year_match.group())
            if not query.effective_year_from <= listing_year <= query.effective_year_to:
                continue
            price = Normalizer.price(cls._text(card, "bull_price"))
            location = cls._text(card, "bull_location") or None
            technical = cls._technical_metadata(subtitle)
            if query.body_type == BodyFilter.ANY:
                body_type = Normalizer.body_type_from_text(f"{title} {subtitle}")
                if body_type is None and body_resolver is not None:
                    body_type = body_resolver(subtitle, listing_year)
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
                    year=listing_year,
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

    def _catalog_body_type(
        self, query: SearchRequest, modification: str, year: int
    ) -> BodyType | None:
        if not hasattr(self.catalog, "generations"):
            return None
        generations = self.catalog.generations(query.brand, query.model, year)
        bodies: set[BodyType] = set()
        compact_modification = re.sub(r"[^a-zа-яё0-9]", "", modification.casefold())
        for generation in generations:
            matched_bodies: set[BodyType] = set()
            for item in generation.get("modifications", []):
                name = re.sub(r"[^a-zа-яё0-9]", "", str(item.get("name", "")).casefold())
                if name and (name in compact_modification or compact_modification in name):
                    raw_body = item.get("body_type")
                    if raw_body:
                        matched_bodies.add(BodyType(str(raw_body)))
            if matched_bodies:
                bodies.update(matched_bodies)
            else:
                bodies.update(BodyType(str(value)) for value in generation.get("body_types", []))
        return next(iter(bodies)) if len(bodies) == 1 else None

    @classmethod
    def parse_discovery(
        cls, content: bytes, body_type: BodyType, diagnostic=None
    ) -> list[CarListing]:
        soup = BeautifulSoup(content, "lxml")
        listings: list[CarListing] = []
        seen: set[str] = set()
        for card in soup.find_all(attrs={"data-ftid": "bulls-list_bull"}):
            if not isinstance(card, Tag):
                continue
            if card.find_parent(attrs={"data-ftid": "bulletin-list_archive"}) is not None:
                continue
            try:
                link = card.find("a", attrs={"data-ftid": "bull_title"}, href=True)
                if not isinstance(link, Tag):
                    raise ValueError("missing title link")
                url = str(link.get("href"))
                external_id = cls._external_id(url)
                if external_id in seen:
                    continue
                title = cls._text(card, "bull_title")
                year_match = _YEAR.search(title)
                if not year_match:
                    raise ValueError("missing year")
                parts = [part for part in urlparse(url).path.split("/") if part]
                if len(parts) < 3:
                    raise ValueError("missing source identity path")
                brand_slug, model_slug = parts[-3], parts[-2]
                brand = brand_slug.replace("_", " ").replace("-", " ")
                name = re.split(r",\s*(?:19|20)\d{2}\b", title, maxsplit=1)[0].strip()
                subtitle = cls._text(card, "bull_subtitle")
                technical = cls._technical_metadata(subtitle)
                identity = Normalizer.marketplace_identity(
                    brand, name, model_slug, subtitle or None
                )
                parsed_body = Normalizer.body_type_from_text(f"{title} {subtitle}") or body_type
                listing = CarListing(
                    source=cls.source,
                    external_id=external_id,
                    brand=identity.brand,
                    model=identity.family_model,
                    modification=identity.modification,
                    fuel_type=technical["fuel_type"],
                    engine_displacement=technical["engine_displacement"],
                    power_hp=technical["power_hp"],
                    drivetrain=technical["drivetrain"],
                    year=int(year_match.group()),
                    body_type=parsed_body,
                    transmission=Normalizer.transmission_from_text(subtitle),
                    price=Normalizer.price(cls._text(card, "bull_price")),
                    url=url,
                    location=cls._text(card, "bull_location") or None,
                    city=cls._text(card, "bull_location") or None,
                    checked_at=datetime.now(UTC),
                    raw_metadata={
                        "title": title,
                        "subtitle": subtitle,
                        "source_brand_slug": brand_slug,
                        "source_model_slug": model_slug,
                        "body_source": "card" if parsed_body != body_type else "route",
                    },
                )
            except (ScraperParseError, ValidationError, ValueError):
                if diagnostic is not None:
                    diagnostic.rejected["parse"] = diagnostic.rejected.get("parse", 0) + 1
                continue
            seen.add(external_id)
            listings.append(listing)
        return listings

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

    @staticmethod
    def _card_count(content: bytes) -> int:
        return len(BeautifulSoup(content, "lxml").find_all(attrs={"data-ftid": "bulls-list_bull"}))

    @staticmethod
    def _next_page_url(content: bytes, current_url: str) -> str | None:
        soup = BeautifulSoup(content, "lxml")
        link = soup.find("link", rel="next") or soup.find("a", rel="next")
        if link and isinstance(link.get("href"), str):
            return urljoin(current_url, str(link["href"]))
        for anchor in soup.find_all("a", href=True):
            label = anchor.get_text(" ", strip=True).casefold()
            if label in {"следующая", "дальше", "next", ">"}:
                return urljoin(current_url, str(anchor["href"]))
        return None

    async def _content_with_fallback(self, response: httpx.Response, url: str, diagnostic) -> bytes:
        try:
            self._detect_interruption(response)
            return response.content
        except (HttpAutomationLimitedError, Http403Error):
            try:
                return await self._browser_content(url, diagnostic)
            except CaptchaRequiredError:
                raise
            except BrowserAccessLimitedError:
                raise
            except Exception as exc:
                raise HttpAutomationLimitedError(
                    "Drom limited direct HTTP and isolated browser fallback failed"
                ) from exc

    async def _browser_content(self, url: str, diagnostic) -> bytes:
        """Avoid launching a fresh browser for every URL after an access denial."""
        async with self._browser_lock:
            if self._browser_error is not None:
                raise self._browser_error
            try:
                diagnostic.browser_fallbacks += 1
                return await self.browser_loader(url)
            except BrowserAccessLimitedError as exc:
                self._browser_error = exc
                raise

    async def _load_in_isolated_browser(self, url: str) -> bytes:
        browser = None
        context = None
        page = None
        try:
            async with async_playwright() as playwright:
                try:
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
                finally:
                    if page is not None:
                        with suppress(PlaywrightError):
                            await page.close()
                    if context is not None:
                        with suppress(PlaywrightError):
                            await context.close()
                    if browser is not None:
                        with suppress(PlaywrightError):
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
