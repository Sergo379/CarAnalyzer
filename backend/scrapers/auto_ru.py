import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError

from backend.config import Settings, get_settings
from backend.models.car import BodyType, MarketDiscoveryRequest, SearchRequest, Transmission
from backend.models.listing import CarListing
from backend.scrapers.base import (
    AuthenticationRequiredError,
    BaseScraper,
    CaptchaRequiredError,
    ScraperParseError,
)
from backend.services.normalizer import Normalizer
from backend.services.regions import auto_ru_region_prefix

_EXTERNAL_ID = re.compile(r"/cars/(?:used|new)/sale/[^/]+/[^/]+/(\d+)-[a-zA-Z0-9]+/?")
_LOCATION = re.compile(
    r"\sв\s([А-ЯЁ][А-Яа-яЁё\- ]{1,60}?)(?=\s+(?:с\s+пробегом|по\s+цене|на\s+Авто)|[:,])",
    re.IGNORECASE,
)
_MODEL_TOKEN = re.compile(r"(?<!\w)(\d{3}[a-zA-Z]{0,3})(?!\w)")
_URL_IDENTITY = re.compile(r"/sale/([^/]+)/([^/]+)/")

# Only verified SEO paths belong here. Unknown models use the brand-wide endpoint and
# are filtered by structured listing names instead of guessing an Auto.ru slug.
_VERIFIED_MODEL_PATHS: dict[tuple[str, str], str] = {
    ("bmw", "520i"): "bmw/5er-520",
    ("bmw", "5series"): "bmw/5er",
    ("bmw", "x5"): "bmw/x5",
    ("audi", "a6"): "audi/a6",
    ("mercedes-benz", "eclass"): "mercedes/e_klasse",
    ("volvo", "s90"): "volvo/s90",
    ("renault", "captur"): "renault/kaptur",
    ("renault", "kaptur"): "renault/kaptur",
    ("ford", "fiesta"): "ford/fiesta",
    ("toyota", "camry"): "toyota/camry",
    ("volkswagen", "golf"): "volkswagen/golf",
}

_AUTO_TRANSMISSIONS: dict[Transmission, str] = {
    Transmission.AUTOMATIC: "AUTOMATIC",
    Transmission.MANUAL: "MECHANICAL",
    Transmission.ROBOT: "ROBOT",
    Transmission.CVT: "VARIATOR",
}

_BODY_WORDS: tuple[tuple[str, BodyType], ...] = (
    ("внедорожник", BodyType.SUV),
    ("кроссовер", BodyType.CROSSOVER),
    ("универсал", BodyType.WAGON),
    ("хэтчбек", BodyType.HATCHBACK),
    ("лифтбек", BodyType.LIFTBACK),
    ("кабриолет", BodyType.CONVERTIBLE),
    ("минивэн", BodyType.MINIVAN),
    ("фургон", BodyType.VAN),
    ("седан", BodyType.SEDAN),
    ("купе", BodyType.COUPE),
    ("пикап", BodyType.PICKUP),
)

_BROAD_BODY_PATHS: dict[BodyType, str] = {
    BodyType.SEDAN: "body-sedan",
    BodyType.WAGON: "body-wagon",
    BodyType.HATCHBACK: "body-hatchback",
    BodyType.LIFTBACK: "body-hatchback",
    BodyType.COUPE: "body-coupe",
    BodyType.CONVERTIBLE: "body-cabrio",
    BodyType.SUV: "body-allroad",
    BodyType.CROSSOVER: "body-allroad",
    BodyType.PICKUP: "body-pickup",
    BodyType.MINIVAN: "body-minivan",
    BodyType.VAN: "body-van",
}


class AutoRuScraper(BaseScraper):
    source = "auto.ru"

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport

    async def search(self, query: SearchRequest) -> list[CarListing]:
        search_url = self.build_search_url(query)
        limits = httpx.Limits(
            max_connections=self.settings.scraper_detail_concurrency,
            max_keepalive_connections=self.settings.scraper_detail_concurrency,
        )
        async with httpx.AsyncClient(
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9",
                # Auto.ru currently returns server-rendered JSON-LD for this minimal,
                # non-spoofed browser identifier. A fabricated full Chrome signature
                # returned only a JavaScript shell during live verification.
                "User-Agent": "Mozilla/5.0",
            },
            follow_redirects=True,
            timeout=self.settings.scraper_timeout_seconds,
            limits=limits,
            transport=self.transport,
        ) as client:
            response = await self._get(client, search_url)
            offers = self.parse_search_offers(response.content)
            if not offers:
                if not self._has_product_json_ld(response.content):
                    raise ScraperParseError(
                        "Auto.ru search response has no server-rendered Product JSON-LD"
                    )
                return []

            semaphore = asyncio.Semaphore(self.settings.scraper_detail_concurrency)
            tasks = [
                self._load_listing(client, semaphore, offer, query)
                for offer in offers[: self.settings.scraper_max_details]
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        listings: list[CarListing] = []
        source_errors: list[Exception] = []
        seen: set[tuple[str, str]] = set()
        for result in results:
            if isinstance(result, Exception):
                source_errors.append(result)
                continue
            if result is None:
                continue
            key = (result.source, result.external_id)
            if key not in seen:
                seen.add(key)
                listings.append(result)

        if not listings and source_errors:
            interruption = next(
                (
                    error
                    for error in source_errors
                    if isinstance(error, (CaptchaRequiredError, AuthenticationRequiredError))
                ),
                None,
            )
            if interruption is not None:
                raise interruption
            first = source_errors[0]
            raise ScraperParseError(
                "Auto.ru returned "
                f"{len(results)} offers, but none could be parsed "
                f"({len(source_errors)} detail errors)"
            ) from first
        return listings

    async def discover(self, query: MarketDiscoveryRequest) -> list[CarListing]:
        limits = httpx.Limits(
            max_connections=self.settings.scraper_detail_concurrency,
            max_keepalive_connections=self.settings.scraper_detail_concurrency,
        )
        async with httpx.AsyncClient(
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "User-Agent": "Mozilla/5.0",
            },
            follow_redirects=True,
            timeout=self.settings.scraper_timeout_seconds,
            limits=limits,
            transport=self.transport,
        ) as client:
            offers: list[dict[str, Any]] = []
            seen_urls: set[str] = set()
            for body_type in sorted(query.body_types, key=str):
                response = await self._get(client, self.build_discovery_url(query, body_type))
                parsed = self.parse_search_offers(response.content)
                if not parsed and not self._has_product_json_ld(response.content):
                    raise ScraperParseError(
                        "Auto.ru broad search has no server-rendered Product JSON-LD"
                    )
                for offer in parsed:
                    url = offer.get("url")
                    if isinstance(url, str) and url not in seen_urls:
                        seen_urls.add(url)
                        offers.append(offer)

            semaphore = asyncio.Semaphore(self.settings.scraper_detail_concurrency)
            tasks = [
                self._load_discovered_listing(client, semaphore, offer, query)
                for offer in offers[: self.settings.scraper_max_details]
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        listings = [result for result in results if isinstance(result, CarListing)]
        if not listings and results and all(isinstance(result, Exception) for result in results):
            first = results[0]
            assert isinstance(first, Exception)
            raise ScraperParseError("Auto.ru broad result details could not be parsed") from first
        return listings

    def build_discovery_url(self, query: MarketDiscoveryRequest, body_type: BodyType) -> str:
        path = _BROAD_BODY_PATHS[body_type]
        region = auto_ru_region_prefix(query.source_vehicle.region)
        transmission = self._transmission_query(query.source_vehicle.transmission)
        return (
            f"{self.settings.auto_ru_base_url.rstrip('/')}/{region}cars/used/{path}/"
            f"?price_from={query.price_from}&price_to={query.price_to}{transmission}"
        )

    def build_search_url(self, query: SearchRequest) -> str:
        brand = self._slug(query.brand)
        model_key = self._model_key(query.modification or query.model)
        path = _VERIFIED_MODEL_PATHS.get((brand, model_key), brand)
        region = auto_ru_region_prefix(query.region)
        transmission = self._transmission_query(query.transmission)
        price_from = round(query.price * 0.8)
        price_to = round(query.price * 1.2)
        return (
            f"{self.settings.auto_ru_base_url.rstrip('/')}/{region}cars/{path}/used/"
            f"?year_from={query.year}&year_to={query.year}"
            f"&price_from={price_from}&price_to={price_to}{transmission}"
        )

    @staticmethod
    def _transmission_query(transmission: Transmission) -> str:
        value = _AUTO_TRANSMISSIONS.get(transmission)
        return f"&transmission={value}" if value else ""

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ScraperParseError(f"Auto.ru request failed: {url}") from exc
        self._detect_interruption(response)
        return response

    @staticmethod
    def _detect_interruption(response: httpx.Response) -> None:
        final_url = str(response.url).casefold()
        if "auth.auto.ru" in final_url or "/login" in final_url:
            raise AuthenticationRequiredError("Auto.ru authentication is required")
        if "showcaptcha" in final_url or "captcha" in final_url:
            raise CaptchaRequiredError("Auto.ru requires a manual CAPTCHA check")
        visible_text = BeautifulSoup(response.content, "lxml").get_text(" ", strip=True).casefold()
        captcha_phrases = (
            "подтвердите, что вы не робот",
            "нам нужно убедиться, что вы не робот",
            "пройдите проверку, чтобы продолжить",
        )
        if any(phrase in visible_text for phrase in captcha_phrases):
            raise CaptchaRequiredError("Auto.ru requires a manual CAPTCHA check")

    @staticmethod
    def parse_search_offers(content: bytes) -> list[dict[str, Any]]:
        soup = BeautifulSoup(content, "lxml")
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            if not script.string:
                continue
            try:
                payload = json.loads(script.string)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("@type") != "Product":
                continue
            aggregate = payload.get("offers")
            if not isinstance(aggregate, dict):
                continue
            offers = aggregate.get("offers")
            if isinstance(offers, list):
                return [offer for offer in offers if isinstance(offer, dict)]
        return []

    @staticmethod
    def _has_product_json_ld(content: bytes) -> bool:
        soup = BeautifulSoup(content, "lxml")
        return AutoRuScraper._product_json_ld(soup) is not None

    async def _load_listing(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        offer: dict[str, Any],
        query: SearchRequest,
    ) -> CarListing | None:
        url = offer.get("url")
        if not isinstance(url, str) or not url.startswith("https://auto.ru/"):
            return None
        async with semaphore:
            response = await self._get(client, url)
        listing = self.parse_detail(response.content, url, query)
        if listing is not None:
            listing.raw_metadata["region_scope"] = query.region.value
            listing.raw_metadata["region_filter_guaranteed"] = query.region.value != "moscow_oblast"
        return listing

    async def _load_discovered_listing(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        offer: dict[str, Any],
        query: MarketDiscoveryRequest,
    ) -> CarListing | None:
        url = offer.get("url")
        if not isinstance(url, str) or not url.startswith("https://auto.ru/"):
            return None
        async with semaphore:
            response = await self._get(client, url)
        listing = self.parse_discovery_detail(response.content, url)
        listing.raw_metadata["region_scope"] = query.source_vehicle.region.value
        listing.raw_metadata["region_filter_guaranteed"] = (
            query.source_vehicle.region.value != "moscow_oblast"
        )
        if listing.body_type not in query.body_types:
            return None
        if not query.price_from <= listing.price <= query.price_to:
            return None
        return listing

    @classmethod
    def parse_discovery_detail(cls, content: bytes, fallback_url: str) -> CarListing:
        soup = BeautifulSoup(content, "lxml")
        product = cls._product_json_ld(soup)
        if product is None:
            raise ScraperParseError(f"Auto.ru Product JSON-LD is missing: {fallback_url}")
        offer = product.get("offers")
        if not isinstance(offer, dict):
            raise ScraperParseError(f"Auto.ru Offer JSON-LD is missing: {fallback_url}")
        url = offer.get("url") if isinstance(offer.get("url"), str) else fallback_url
        raw_name = str(product.get("name") or offer.get("name") or "")
        brand_value = product.get("brand")
        if isinstance(brand_value, dict):
            brand_value = brand_value.get("name")
        match = _URL_IDENTITY.search(urlparse(url).path)
        if match is None:
            raise ScraperParseError(f"Auto.ru make/model path is missing: {url}")
        identity = Normalizer.marketplace_identity(
            str(brand_value or match.group(1)), raw_name, match.group(2)
        )
        metadata = cls._metadata_text(soup)
        location, city, region = cls._location_details(soup, product)
        try:
            return CarListing(
                source=cls.source,
                external_id=cls._external_id(url),
                brand=identity.brand,
                model=identity.family_model,
                modification=identity.modification,
                year=cls._year(product.get("productionDate")),
                body_type=cls._body_type(metadata),
                transmission=Normalizer.transmission_from_text(
                    f"{metadata} {product.get('description', '')}"
                ),
                price=offer.get("price"),
                url=urljoin(fallback_url, url),
                location=location,
                city=city,
                region=region,
                checked_at=datetime.now(UTC),
                raw_metadata={
                    "marketplace_name": raw_name,
                    "availability": offer.get("availability"),
                    "price_currency": offer.get("priceCurrency"),
                },
            )
        except ValidationError as exc:
            raise ScraperParseError(f"Invalid Auto.ru listing: {fallback_url}") from exc

    @classmethod
    def parse_detail(
        cls,
        content: bytes,
        fallback_url: str,
        query: SearchRequest,
    ) -> CarListing | None:
        soup = BeautifulSoup(content, "lxml")
        product = cls._product_json_ld(soup)
        if product is None:
            raise ScraperParseError(f"Auto.ru Product JSON-LD is missing: {fallback_url}")

        offer = product.get("offers")
        if not isinstance(offer, dict):
            raise ScraperParseError(f"Auto.ru Offer JSON-LD is missing: {fallback_url}")
        url = offer.get("url") if isinstance(offer.get("url"), str) else fallback_url
        external_id = cls._external_id(url)
        name = str(product.get("name") or offer.get("name") or "")
        brand_value = product.get("brand")
        if isinstance(brand_value, dict):
            brand_value = brand_value.get("name")
        brand = Normalizer.brand(str(brand_value or query.brand))
        year = cls._year(product.get("productionDate"))
        price = offer.get("price")
        meta_text = cls._metadata_text(soup)
        body_type = cls._body_type(meta_text)
        location, city, region = cls._location_details(soup, product)
        model = cls._canonical_model(name, query)
        requested_model = query.modification or query.model

        if brand.casefold() != Normalizer.brand(query.brand).casefold():
            return None
        if year != query.year:
            return None
        if not cls._model_matches(name, requested_model):
            return None

        try:
            return CarListing(
                source=cls.source,
                external_id=external_id,
                brand=brand,
                model=model,
                modification=name,
                year=year,
                body_type=body_type,
                transmission=Normalizer.transmission_from_text(
                    f"{meta_text} {product.get('description', '')}"
                ),
                price=price,
                url=urljoin(fallback_url, url),
                location=location,
                city=city,
                region=region,
                checked_at=datetime.now(UTC),
                raw_metadata={
                    "marketplace_name": name,
                    "availability": offer.get("availability"),
                    "price_currency": offer.get("priceCurrency"),
                },
            )
        except ValidationError as exc:
            raise ScraperParseError(f"Invalid Auto.ru listing: {fallback_url}") from exc

    @staticmethod
    def _product_json_ld(soup: BeautifulSoup) -> dict[str, Any] | None:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            if not script.string:
                continue
            try:
                payload = json.loads(script.string)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("@type") == "Product":
                return payload
        return None

    @staticmethod
    def _metadata_text(soup: BeautifulSoup) -> str:
        values: list[str] = []
        if soup.title:
            values.append(soup.title.get_text(" ", strip=True))
        for attrs in ({"name": "description"}, {"property": "og:title"}):
            tag = soup.find("meta", attrs=attrs)
            if tag and isinstance(tag.get("content"), str):
                values.append(tag["content"])
        return " ".join(values)

    @staticmethod
    def _body_type(text: str) -> BodyType:
        folded = text.casefold()
        for word, body_type in _BODY_WORDS:
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", folded):
                return body_type
        raise ScraperParseError("Auto.ru body type is missing from page metadata")

    @staticmethod
    def _location_details(
        soup: BeautifulSoup, product: dict[str, Any]
    ) -> tuple[str | None, str | None, str | None]:
        metadata = AutoRuScraper._metadata_text(soup)
        match = _LOCATION.search(metadata)
        city = Normalizer.text(match.group(1)) if match else None
        description = str(product.get("description") or "")
        if city is None:
            address = re.search(
                r"(?:по\s+адресу\s*:\s*)?(?:г\.|город)\s*"
                r"([А-ЯЁ][А-Яа-яЁё\- ]{1,50}?)(?=[,\n])",
                description,
                re.I,
            )
            city = Normalizer.text(address.group(1)) if address else None
        city_aliases = {
            "москве": "Москва",
            "владивостоке": "Владивосток",
            "санкт-петербурге": "Санкт-Петербург",
        }
        if city:
            city = city_aliases.get(city.casefold(), city)
        explicit_region = re.search(
            r"\b([А-ЯЁ][А-Яа-яЁё\- ]+\s+область)\b", f"{metadata} {description}", re.I
        )
        region = Normalizer.text(explicit_region.group(1)) if explicit_region else None
        location = ", ".join(value for value in (city, region) if value) or None
        return location, city, region

    @staticmethod
    def _year(value: Any) -> int:
        match = re.search(r"\b(19|20)\d{2}\b", str(value))
        if not match:
            raise ScraperParseError("Auto.ru production year is missing")
        return int(match.group())

    @staticmethod
    def _external_id(url: str) -> str:
        match = _EXTERNAL_ID.search(urlparse(url).path)
        if not match:
            raise ScraperParseError(f"Auto.ru external id is missing: {url}")
        return match.group(1)

    @staticmethod
    def _canonical_model(name: str, query: SearchRequest) -> str:
        requested = query.modification or query.model
        if AutoRuScraper._model_matches(name, requested):
            return Normalizer.model(query.model)
        token = _MODEL_TOKEN.search(name)
        return token.group(1) if token else Normalizer.model(query.model)

    @staticmethod
    def _model_matches(name: str, model: str) -> bool:
        compact_name = re.sub(r"[^a-zа-я0-9]", "", name.casefold())
        compact_model = re.sub(r"[^a-zа-я0-9]", "", model.casefold())
        if compact_model == "eclass":
            return "eclass" in compact_name or "eкласс" in compact_name
        if compact_model == "captur":
            return "captur" in compact_name or "kaptur" in compact_name
        if compact_model == "5series":
            return "5series" in compact_name or "5серии" in compact_name
        return bool(compact_model) and compact_model in compact_name

    @staticmethod
    def _model_key(model: str) -> str:
        return re.sub(r"[^a-z0-9]", "", model.casefold())

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        if not slug:
            raise ValueError(f"Auto.ru cannot build a slug from {value!r}")
        return slug
