import asyncio
import json
import re
import time
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
from backend.services.marketplace_catalog import CatalogCache
from backend.services.normalizer import Normalizer
from backend.services.regions import auto_ru_region_prefix

_EXTERNAL_ID = re.compile(r"/cars/(?:used|new)/sale/[^/]+/[^/]+/(\d+)-[a-zA-Z0-9]+/?")
_LOCATION = re.compile(
    r"\sв\s([А-ЯЁ][А-Яа-яЁё\- ]{1,60}?)(?=\s+(?:с\s+пробегом|по\s+цене|на\s+Авто)|[:,])",
    re.IGNORECASE,
)
_MODEL_TOKEN = re.compile(r"(?<!\w)(\d{3}[a-zA-Z]{0,3})(?!\w)")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_URL_IDENTITY = re.compile(r"/sale/([^/]+)/([^/]+)/")

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


class _ListingRejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AutoRuScraper(BaseScraper):
    source = "auto.ru"

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        catalog: CatalogCache | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        self.catalog = catalog or CatalogCache()

    async def search(self, query: SearchRequest) -> list[CarListing]:
        started = time.perf_counter()
        search_url = self.build_search_url(query)
        diagnostic = self.reset_diagnostics("target", search_url)
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
            offers = await self._collect_offers(client, search_url, diagnostic)
            if not offers:
                return []

            fast, fallback = self._parse_target_cards(offers, query, diagnostic)
            diagnostic.parsed_count += len(fast)
            semaphore = asyncio.Semaphore(self.settings.scraper_detail_concurrency)
            diagnostic.detail_requests += len(fallback)
            tasks = [self._load_listing(client, semaphore, offer, query) for offer in fallback]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        listings: list[CarListing] = []
        source_errors: list[Exception] = []
        seen: set[tuple[str, str]] = set()
        for result in [*fast, *results]:
            if isinstance(result, _ListingRejected):
                diagnostic.rejected[result.reason] = diagnostic.rejected.get(result.reason, 0) + 1
                continue
            if isinstance(result, Exception):
                source_errors.append(result)
                diagnostic.rejected["parse"] = diagnostic.rejected.get("parse", 0) + 1
                continue
            if result is None:
                continue
            key = (result.source, result.external_id)
            if key not in seen:
                seen.add(key)
                listings.append(result)
        diagnostic.parsed_count += len(results) - len(source_errors)
        diagnostic.accepted_count = len(listings)
        diagnostic.elapsed_seconds = time.perf_counter() - started

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
        started = time.perf_counter()
        diagnostic = self.reset_diagnostics("competitors")
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
            body_types: tuple[BodyType | None, ...] = (
                (None,)
                if query.body_types == frozenset(BodyType)
                else tuple(sorted(query.body_types, key=str))
            )
            discovery_urls = list(
                dict.fromkeys(
                    self.build_discovery_url(query, body_type) for body_type in body_types
                )
            )
            if discovery_urls:
                diagnostic.resolved_url = discovery_urls[0]
            batches = await asyncio.gather(
                *(self._collect_offers(client, url, diagnostic) for url in discovery_urls)
            )
            for parsed in batches:
                for offer in parsed:
                    url = offer.get("url")
                    if isinstance(url, str) and url not in seen_urls:
                        seen_urls.add(url)
                        offers.append(offer)

            fast, fallback = self._parse_discovery_cards(offers, query, diagnostic)
            semaphore = asyncio.Semaphore(self.settings.scraper_detail_concurrency)
            diagnostic.detail_requests += len(fallback)
            tasks = [
                self._load_discovered_listing(client, semaphore, offer, query) for offer in fallback
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        listings: list[CarListing] = []
        detail_errors: list[Exception] = []
        for result in [*fast, *results]:
            if isinstance(result, _ListingRejected):
                diagnostic.rejected[result.reason] = diagnostic.rejected.get(result.reason, 0) + 1
            elif isinstance(result, Exception):
                detail_errors.append(result)
            elif isinstance(result, CarListing):
                listings.append(result)
        diagnostic.parsed_count += len(results) - len(detail_errors)
        diagnostic.accepted_count = len(listings)
        diagnostic.elapsed_seconds = time.perf_counter() - started
        diagnostic.rejected["parse"] = len(detail_errors)
        if not listings and detail_errors and len(detail_errors) == len(results):
            raise ScraperParseError(
                "Auto.ru broad result details could not be parsed"
            ) from detail_errors[0]
        return listings

    def build_discovery_url(self, query: MarketDiscoveryRequest, body_type: BodyType | None) -> str:
        path = f"{_BROAD_BODY_PATHS[body_type]}/" if body_type else ""
        region = auto_ru_region_prefix(query.source_vehicle.region)
        transmission = self._transmission_query(query.source_vehicle.transmission)
        return (
            f"{self.settings.auto_ru_base_url.rstrip('/')}/{region}cars/used/{path}"
            f"?price_from={query.price_from}&price_to={query.price_to}{transmission}"
        )

    def build_search_url(self, query: SearchRequest) -> str:
        identity = self.catalog.resolve_identity(query.brand, query.model)
        brand = self._slug(identity.brand)
        ref = self.catalog.source_model_ref("auto.ru", identity.brand, identity.model)
        path = brand
        if ref is not None:
            parts = [part for part in ref.path.split("/") if part]
            if "cars" in parts:
                source_parts = parts[parts.index("cars") + 1 :]
                if source_parts:
                    path = "/".join(source_parts)
        region = auto_ru_region_prefix(query.region)
        transmission = self._transmission_query(query.transmission)
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
            f"{self.settings.auto_ru_base_url.rstrip('/')}/{region}cars/{path}/used/"
            f"?year_from={query.effective_year_from}&year_to={query.effective_year_to}"
            f"&price_from={price_from}&price_to={price_to}{transmission}"
        )

    async def _collect_offers(
        self,
        client: httpx.AsyncClient,
        initial_url: str,
        diagnostic,
    ) -> list[dict[str, Any]]:
        offers: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        url: str | None = initial_url
        for _ in range(self.settings.scraper_max_pages):
            if url is None:
                break
            response = await self._get(client, url)
            diagnostic.pages_scanned += 1
            parsed = self.parse_search_offers(response.content)
            self._attach_card_data(parsed, response.content)
            diagnostic.raw_count += len(parsed)
            new_count = 0
            for offer in parsed:
                offer_url = offer.get("url")
                if isinstance(offer_url, str) and offer_url not in seen_urls:
                    seen_urls.add(offer_url)
                    offers.append(offer)
                    new_count += 1
                    if len(offers) >= self.settings.scraper_max_listings:
                        return offers
            if not parsed or new_count == 0:
                break
            url = self._next_page_url(response.content, str(response.url))
        return offers

    @classmethod
    def _attach_card_data(cls, offers: list[dict[str, Any]], content: bytes) -> None:
        soup = BeautifulSoup(content, "lxml")
        cards: dict[str, dict[str, str]] = {}
        for card in soup.find_all(attrs={"data-seo": "listing-item"}):
            anchor = next(
                (item for item in card.find_all("a", href=True) if "/sale/" in str(item["href"])),
                None,
            )
            if anchor is None:
                continue
            url = str(anchor["href"])
            address = card.find(class_=lambda value: value and "sellerAddress" in str(value))
            cards[url] = {
                "name": anchor.get_text(" ", strip=True),
                "text": card.get_text(" ", strip=True),
                "location": address.get_text(" ", strip=True) if address else "",
            }
        for offer in offers:
            url = offer.get("url")
            if isinstance(url, str) and url in cards:
                offer.update(cards[url])

    @classmethod
    def _parse_target_cards(
        cls, offers: list[dict[str, Any]], query: SearchRequest, diagnostic
    ) -> tuple[list[CarListing], list[dict[str, Any]]]:
        accepted: list[CarListing] = []
        fallback: list[dict[str, Any]] = []
        for offer in offers:
            try:
                listing = cls._listing_from_card(offer, query)
            except _ListingRejected as exc:
                diagnostic.parsed_count += 1
                diagnostic.rejected[exc.reason] = diagnostic.rejected.get(exc.reason, 0) + 1
                continue
            if listing is None:
                fallback.append(offer)
            else:
                accepted.append(listing)
        return accepted, fallback

    @classmethod
    def _parse_discovery_cards(
        cls, offers: list[dict[str, Any]], query: MarketDiscoveryRequest, diagnostic
    ) -> tuple[list[CarListing], list[dict[str, Any]]]:
        accepted: list[CarListing] = []
        fallback: list[dict[str, Any]] = []
        for offer in offers:
            url = offer.get("url")
            name = str(offer.get("name") or "")
            text = str(offer.get("text") or "")
            if not isinstance(url, str) or not name or not text:
                fallback.append(offer)
                continue
            try:
                match = _URL_IDENTITY.search(urlparse(url).path)
                if match is None:
                    raise ValueError
                identity = Normalizer.marketplace_identity(match.group(1), name, match.group(2))
                listing = CarListing(
                    source=cls.source,
                    external_id=cls._external_id(url),
                    brand=identity.brand,
                    model=identity.family_model,
                    modification=identity.modification,
                    year=cls._card_year(name),
                    body_type=cls._body_type(text),
                    transmission=Normalizer.transmission_from_text(text),
                    price=offer.get("price"),
                    url=url,
                    location=cls._card_location(str(offer.get("location") or "")),
                    checked_at=datetime.now(UTC),
                    raw_metadata={"marketplace_name": name},
                )
            except (ValueError, ValidationError, ScraperParseError):
                fallback.append(offer)
                continue
            diagnostic.parsed_count += 1
            if listing.body_type not in query.body_types:
                diagnostic.rejected["body"] = diagnostic.rejected.get("body", 0) + 1
                continue
            if not query.price_from <= listing.price <= query.price_to:
                diagnostic.rejected["price"] = diagnostic.rejected.get("price", 0) + 1
                continue
            listing.raw_metadata.update(
                {
                    "region_scope": query.source_vehicle.region.value,
                    "region_filter_guaranteed": query.source_vehicle.region.value
                    != "moscow_oblast",
                }
            )
            accepted.append(listing)
        return accepted, fallback

    @classmethod
    def _listing_from_card(cls, offer: dict[str, Any], query: SearchRequest) -> CarListing | None:
        url = offer.get("url")
        name = str(offer.get("name") or "")
        text = str(offer.get("text") or "")
        if not isinstance(url, str) or not name or not text:
            return None
        try:
            year = cls._card_year(name)
            body_type = cls._body_type(text)
            price = int(offer["price"])
        except (KeyError, TypeError, ValueError, ScraperParseError):
            return None
        ref = cls._model_matches(name, query.modification or query.model) or cls._model_matches(
            name, query.model
        )
        if not ref:
            raise _ListingRejected("model")
        if not query.effective_year_from <= year <= query.effective_year_to:
            raise _ListingRejected("year")
        if not (
            query.effective_price_from <= price <= query.effective_price_to
            if query.price_mode.value == "range"
            else round(query.reference_price * 0.8) <= price <= round(query.reference_price * 1.2)
        ):
            raise _ListingRejected("price")
        transmission = Normalizer.transmission_from_text(text)
        if query.transmission != Transmission.ANY and transmission != query.transmission:
            raise _ListingRejected("transmission")
        location = cls._card_location(str(offer.get("location") or ""))
        if query.region.value != "any" and location is None:
            return None
        return CarListing(
            source=cls.source,
            external_id=cls._external_id(url),
            brand=Normalizer.brand(query.brand),
            model=Normalizer.model(query.model),
            modification=name,
            year=year,
            body_type=body_type,
            transmission=transmission,
            price=price,
            url=url,
            location=location,
            city=location,
            checked_at=datetime.now(UTC),
            raw_metadata={
                "marketplace_name": name,
                "region_scope": query.region.value,
                "region_filter_guaranteed": query.region.value != "moscow_oblast",
            },
        )

    @staticmethod
    def _card_year(name: str) -> int:
        match = _YEAR.search(name)
        if match is None:
            raise ValueError("card year missing")
        return int(match.group())

    @staticmethod
    def _card_location(value: str) -> str | None:
        value = Normalizer.text(value)
        if not value:
            return None
        return value.split(",", 1)[0].strip() or None

    @staticmethod
    def _next_page_url(content: bytes, current_url: str) -> str | None:
        soup = BeautifulSoup(content, "lxml")
        link = soup.find("link", rel="next") or soup.find("a", rel="next")
        if link and isinstance(link.get("href"), str):
            return urljoin(current_url, link["href"])
        for anchor in soup.find_all("a", href=True):
            label = anchor.get_text(" ", strip=True).casefold()
            aria = str(anchor.get("aria-label", "")).casefold()
            if label in {"следующая", "дальше", "next", ">"} or "следующ" in aria:
                return urljoin(current_url, str(anchor["href"]))
        return None

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
        ref = self.catalog.source_model_ref("auto.ru", query.brand, query.model)
        aliases = (ref.slug,) if ref is not None and ref.slug else ()
        listing = self.parse_detail(response.content, url, query, aliases)
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
            raise _ListingRejected("body")
        if not query.price_from <= listing.price <= query.price_to:
            raise _ListingRejected("price")
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
        model_aliases: tuple[str, ...] = (),
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
            raise _ListingRejected("model")
        if not query.effective_year_from <= year <= query.effective_year_to:
            raise _ListingRejected("year")
        if not any(
            cls._model_matches(name, candidate)
            for candidate in (requested_model, query.model, *model_aliases)
        ):
            raise _ListingRejected("model")

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
        def compact(value: str) -> str:
            normalized = value.casefold().replace("серии", "series").replace("серия", "series")
            normalized = normalized.replace("класс", "class")
            return re.sub(r"[^a-zа-я0-9]", "", normalized)

        compact_name = compact(name)
        compact_model = compact(model)
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
