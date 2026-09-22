import re
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import ValidationError

from backend.config import Settings, get_settings
from backend.models.car import BodyType, MarketDiscoveryRequest, SearchRequest
from backend.models.listing import CarListing
from backend.scrapers.base import (
    AuthenticationRequiredError,
    BaseScraper,
    CaptchaRequiredError,
    Http403Error,
    Http429Error,
    ScraperParseError,
)
from backend.services.normalizer import Normalizer
from backend.services.regions import drom_region_prefix, drom_region_query

_VERIFIED_MODEL_PATHS: dict[tuple[str, str], str] = {
    ("bmw", "520i"): "bmw/5-series",
    ("bmw", "5series"): "bmw/5-series",
    ("bmw", "x5"): "bmw/x5",
    ("audi", "a6"): "audi/a6",
    ("mercedes-benz", "eclass"): "mercedes-benz/e-class",
    ("volvo", "s90"): "volvo/s90",
    ("renault", "captur"): "renault/kaptur",
    ("renault", "kaptur"): "renault/kaptur",
    ("ford", "fiesta"): "ford/fiesta",
    ("toyota", "camry"): "toyota/camry",
    ("volkswagen", "golf"): "volkswagen/golf",
}
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
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport

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
        self._detect_interruption(response)
        return self.parse_search(response.content, query)

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
                    response = await client.get(self.build_discovery_url(query, body_type))
                except httpx.HTTPError as exc:
                    raise ScraperParseError("Drom broad request failed") from exc
                self._detect_interruption(response)
                for listing in self.parse_discovery(response.content, body_type):
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
        brand = self._slug(query.brand)
        requested_model = query.modification or query.model
        model = self._model_key(requested_model)
        path = _VERIFIED_MODEL_PATHS.get((brand, model))
        if path is None:
            raise ScraperParseError(
                f"Drom model path is not verified for {query.brand} {query.model}"
            )
        region = drom_region_prefix(query.region)
        region_query = drom_region_query(query.region)
        return (
            f"https://auto.drom.ru/{region}{path}/year-{query.year}/used/"
            f"{query.body_type.value}/?unsold=1&minprice={round(query.price * 0.8)}"
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
            try:
                listing = CarListing(
                    source=cls.source,
                    external_id=external_id,
                    brand=Normalizer.brand(query.brand),
                    model=Normalizer.model(query.model),
                    modification=subtitle or None,
                    year=query.year,
                    body_type=query.body_type,
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
            raise Http429Error("Drom rate limit: HTTP 429")
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
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        if not slug:
            raise ValueError(f"Drom cannot build a slug from {value!r}")
        return slug
