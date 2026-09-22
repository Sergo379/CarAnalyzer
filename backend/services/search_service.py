import asyncio
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from backend.models.car import (
    BodyFilter,
    BodyType,
    MarketDiscoveryRequest,
    SearchRequest,
    SourceVehicle,
    Transmission,
)
from backend.models.listing import (
    CarListing,
    CategoryResult,
    ClassifiedListing,
    ModelGroup,
)
from backend.scrapers.auto_ru import AutoRuScraper
from backend.scrapers.avito import AvitoScraper
from backend.scrapers.base import (
    AuthenticationRequiredError,
    BaseScraper,
    BrowserAccessLimitedError,
    CaptchaRequiredError,
    Http403Error,
    Http429Error,
    HttpAutomationLimitedError,
    RobotsRestrictedError,
    ScraperError,
    ScraperParseError,
    SourceBlockedError,
    UnsupportedQueryError,
)
from backend.scrapers.drom import DromScraper
from backend.services.catalog import VehicleCatalog
from backend.services.competitor_engine import CompetitorEngine
from backend.services.deduplication import deduplicate_listings
from backend.services.grouping import group_by_model
from backend.services.knowledge import CarKnowledgeService
from backend.services.marketplace_catalog import CatalogCache
from backend.services.normalizer import Normalizer
from backend.services.regions import location_matches

logger = logging.getLogger(__name__)


class SourceState(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    CAPTCHA_REQUIRED = "captcha_required"
    AUTH_REQUIRED = "auth_required"
    HTTP_429 = "http_429"
    HTTP_403 = "http_403"
    HTTP_AUTOMATION_LIMITED = "http_automation_limited"
    BROWSER_ACCESS_LIMITED = "browser_access_limited"
    ROBOTS_RESTRICTED = "robots_restricted"
    UNSUPPORTED_QUERY = "unsupported_query"
    PARSE_ERROR = "parse_error"
    BLOCKED = "blocked"
    ERROR = "error"


class SearchResult(BaseModel):
    source_vehicle: SourceVehicle
    source_status: dict[str, SourceState]
    source_details: dict[str, str] = Field(default_factory=dict)
    source_listings: CategoryResult = Field(default_factory=CategoryResult)
    source_model_group: ModelGroup | None = None
    direct: CategoryResult
    expensive: CategoryResult
    cheaper: CategoryResult
    car_knowledge: dict[str, dict[str, object]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class MarketplaceOutcome:
    discovered: list[CarListing] | BaseException
    source_listings: list[CarListing] | BaseException


class SearchService:
    def __init__(
        self,
        scrapers: Iterable[BaseScraper] | None = None,
        catalog: VehicleCatalog | None = None,
        engine: CompetitorEngine | None = None,
        knowledge: CarKnowledgeService | None = None,
    ) -> None:
        self.scrapers = (
            list(scrapers)
            if scrapers is not None
            else [AutoRuScraper(), AvitoScraper(), DromScraper()]
        )
        self.catalog = catalog or VehicleCatalog()
        self.engine = engine or CompetitorEngine()
        self.knowledge = knowledge or CarKnowledgeService()
        self.marketplace_catalog = CatalogCache()

    async def search(self, request: SearchRequest) -> SearchResult:
        identity = Normalizer.vehicle_identity(request.brand, request.model)
        selected_modification = self.marketplace_catalog.modification(request.modification_id)
        modification = (
            str(selected_modification["name"])
            if selected_modification
            else request.modification or identity.modification
        )
        normalized = SearchRequest(
            brand=identity.brand,
            model=identity.family_model,
            modification=modification,
            year=request.year,
            body_type=request.body_type,
            transmission=request.transmission,
            region=request.region,
            price=request.price,
            generation_id=request.generation_id,
            modification_id=request.modification_id,
        )
        concrete_body = (
            None if normalized.body_type == BodyFilter.ANY else BodyType(normalized.body_type.value)
        )
        source_segment = (
            self._classify_segment(
                normalized.brand,
                normalized.model,
                normalized.year,
                concrete_body,
                normalized.price,
            )
            if concrete_body
            else None
        )
        generation = next(
            (
                str(item["name"])
                for item in self.marketplace_catalog.generations(
                    normalized.brand, normalized.model, normalized.year
                )
                if item["id"] == normalized.generation_id
            ),
            None,
        )
        source = SourceVehicle(
            **normalized.model_dump(), segment=source_segment, generation=generation
        )
        ranges = self.engine.calculate_price_ranges(normalized.price)
        discovery = MarketDiscoveryRequest(
            source_vehicle=normalized,
            price_from=ranges.cheaper_min,
            price_to=ranges.expensive_max,
            body_types=(
                frozenset(BodyType)
                if normalized.body_type == BodyFilter.ANY
                else self.engine.body_compatibility.get(
                    BodyType(normalized.body_type.value),
                    frozenset({BodyType(normalized.body_type.value)}),
                )
            ),
        )

        outcomes = await asyncio.gather(
            *(self._search_marketplace(scraper, discovery, normalized) for scraper in self.scrapers)
        )
        statuses: dict[str, SourceState] = {}
        details: dict[str, str] = {}
        warnings: list[str] = []
        discovered: list[CarListing] = []
        target_listings: list[CarListing] = []
        for scraper, outcome in zip(self.scrapers, outcomes, strict=True):
            errors = [
                value
                for value in (outcome.discovered, outcome.source_listings)
                if isinstance(value, BaseException)
            ]
            successes = [
                value
                for value in (outcome.discovered, outcome.source_listings)
                if isinstance(value, list)
            ]
            if successes:
                count = sum(len(value) for value in successes)
                statuses[scraper.source] = SourceState.OK if count else SourceState.EMPTY
                if errors:
                    details[scraper.source] = "Источник работает частично"
                else:
                    details[scraper.source] = (
                        "Работает" if count else "Работает, подходящих объявлений нет"
                    )
            else:
                error = self._most_specific_error(errors)
                statuses[scraper.source] = self._state_for_error(error)
                details[scraper.source] = self._message_for_state(statuses[scraper.source])
                warnings.append(
                    f"{self._display_source(scraper.source)} — {details[scraper.source]}"
                )
            if isinstance(outcome.discovered, list):
                discovered.extend(outcome.discovered)
            if isinstance(outcome.source_listings, list):
                target_listings.extend(outcome.source_listings)
            logger.info(
                "marketplace source=%s state=%s discovered=%s target=%s errors=%s",
                scraper.source,
                statuses[scraper.source],
                len(outcome.discovered) if isinstance(outcome.discovered, list) else 0,
                len(outcome.source_listings) if isinstance(outcome.source_listings, list) else 0,
                ",".join(type(error).__name__ for error in errors) or "none",
            )

        source_unique = [
            listing
            for listing in deduplicate_listings(target_listings)
            if self._matches_source(listing, normalized)
        ]
        source_items = [self._classified_item(source.price, listing) for listing in source_unique]

        competitors = []
        for listing in deduplicate_listings(discovered):
            if self._same_family(listing.brand, listing.model, source.brand, source.model):
                continue
            if not self._matches_user_filters(listing, normalized):
                continue
            listing.segment = self._classify_segment(
                listing.brand,
                listing.model,
                listing.year,
                listing.body_type,
                listing.price,
            )
            competitors.append(listing)
        groups = self.engine.classify(source, competitors)
        categorized = groups.direct + groups.expensive + groups.cheaper
        knowledge: dict[str, dict[str, object]] = {}
        vehicles = source_unique + [item.listing for item in categorized]
        source_key = (
            f"{source.brand}|{source.model}|{source.generation}|{source.year}"
            if source.generation
            else f"{source.brand}|{source.model}|{source.year}"
        )
        knowledge[source_key] = self.knowledge.get(
            source.brand,
            source.model,
            source.year,
            concrete_body,
            source.segment,
            generation=source.generation or "",
        ).model_dump(mode="json")
        for vehicle in vehicles:
            key = f"{vehicle.brand}|{vehicle.model}|{vehicle.year}"
            if key in knowledge:
                continue
            knowledge[key] = self.knowledge.get(
                vehicle.brand,
                vehicle.model,
                vehicle.year,
                vehicle.body_type,
                vehicle.segment,
            ).model_dump(mode="json")

        source_groups = group_by_model(source_items)
        return SearchResult(
            source_vehicle=source,
            source_status=statuses,
            source_details=details,
            source_listings=CategoryResult(
                listings=source_items,
                model_groups=source_groups,
            ),
            source_model_group=source_groups[0] if source_groups else None,
            direct=CategoryResult(
                listings=groups.direct, model_groups=group_by_model(groups.direct)
            ),
            expensive=CategoryResult(
                listings=groups.expensive, model_groups=group_by_model(groups.expensive)
            ),
            cheaper=CategoryResult(
                listings=groups.cheaper, model_groups=group_by_model(groups.cheaper)
            ),
            car_knowledge=knowledge,
            warnings=warnings,
        )

    @staticmethod
    async def _search_marketplace(
        scraper: BaseScraper,
        discovery: MarketDiscoveryRequest,
        source_query: SearchRequest,
    ) -> MarketplaceOutcome:
        discovered, source_listings = await asyncio.gather(
            scraper.discover(discovery),
            scraper.search(source_query),
            return_exceptions=True,
        )
        return MarketplaceOutcome(discovered=discovered, source_listings=source_listings)

    def _matches_source(self, listing: CarListing, query: SearchRequest) -> bool:
        return (
            self._same_family(listing.brand, listing.model, query.brand, query.model)
            and listing.year == query.year
            and (
                query.body_type == BodyFilter.ANY
                or self.engine.body_is_compatible(
                    BodyType(query.body_type.value), listing.body_type
                )
            )
            and self._matches_user_filters(listing, query)
            and (
                not query.modification_id
                or bool(listing.modification)
                and self._same_modification(listing.modification, query.modification or "")
            )
        )

    @staticmethod
    def _matches_user_filters(listing: CarListing, query: SearchRequest) -> bool:
        if query.transmission != Transmission.ANY and listing.transmission != query.transmission:
            return False
        if (
            listing.raw_metadata.get("region_filter_guaranteed") is True
            and listing.raw_metadata.get("region_scope") == query.region.value
        ):
            return True
        return location_matches(query.region, listing.location, listing.city, listing.region)

    @staticmethod
    def _same_family(brand_a: str, model_a: str, brand_b: str, model_b: str) -> bool:
        def compact(value: str) -> str:
            return re.sub(r"[^a-zа-яё0-9]", "", value.casefold())

        left = Normalizer.vehicle_identity(brand_a, model_a)
        right = Normalizer.vehicle_identity(brand_b, model_b)
        left_model = compact(left.family_model).replace("kaptur", "captur")
        right_model = compact(right.family_model).replace("kaptur", "captur")
        return compact(left.brand) == compact(right.brand) and left_model == right_model

    @staticmethod
    def _same_modification(listing_value: str, requested: str) -> bool:
        def compact(value: str) -> str:
            return re.sub(r"[^a-zа-яё0-9]", "", value.casefold())

        needle = compact(requested.split(" AT ", 1)[0])
        return bool(needle) and needle in compact(listing_value)

    @staticmethod
    def _classified_item(source_price: int, listing: CarListing) -> ClassifiedListing:
        difference = listing.price - source_price
        return ClassifiedListing(
            listing=listing,
            price_difference=difference,
            price_difference_percent=round(difference / source_price * 100, 2),
        )

    def _classify_segment(
        self,
        brand: str,
        model: str,
        year: int,
        body_type: BodyType,
        market_price: int,
    ) -> str | None:
        try:
            entry = self.catalog.classify(brand, model, year, body_type, market_price)
        except Exception as exc:
            logger.warning("vehicle_classification error_type=%s", type(exc).__name__)
            return None
        return entry.segment

    @classmethod
    def _most_specific_error(cls, errors: list[BaseException]) -> BaseException:
        priority = (
            CaptchaRequiredError,
            BrowserAccessLimitedError,
            HttpAutomationLimitedError,
            Http429Error,
            Http403Error,
            AuthenticationRequiredError,
            UnsupportedQueryError,
            RobotsRestrictedError,
            ScraperParseError,
            SourceBlockedError,
            ScraperError,
        )
        for error_type in priority:
            for error in errors:
                if isinstance(error, error_type):
                    return error
        return errors[0] if errors else RuntimeError("unknown source failure")

    @staticmethod
    def _state_for_error(error: BaseException) -> SourceState:
        if isinstance(error, CaptchaRequiredError):
            return SourceState.CAPTCHA_REQUIRED
        if isinstance(error, Http429Error):
            return SourceState.HTTP_429
        if isinstance(error, HttpAutomationLimitedError):
            return SourceState.HTTP_AUTOMATION_LIMITED
        if isinstance(error, BrowserAccessLimitedError):
            return SourceState.BROWSER_ACCESS_LIMITED
        if isinstance(error, Http403Error):
            return SourceState.HTTP_403
        if isinstance(error, AuthenticationRequiredError):
            return SourceState.AUTH_REQUIRED
        if isinstance(error, UnsupportedQueryError):
            return SourceState.UNSUPPORTED_QUERY
        if isinstance(error, RobotsRestrictedError):
            return SourceState.ROBOTS_RESTRICTED
        if isinstance(error, ScraperParseError):
            return SourceState.PARSE_ERROR
        if isinstance(error, SourceBlockedError):
            return SourceState.BLOCKED
        return SourceState.ERROR

    @staticmethod
    def _message_for_state(state: SourceState) -> str:
        return {
            SourceState.CAPTCHA_REQUIRED: "требуется ручная проверка CAPTCHA",
            SourceState.HTTP_429: "площадка ограничила автоматический HTTP-доступ (HTTP 429)",
            SourceState.HTTP_403: "доступ отклонён площадкой (HTTP 403)",
            SourceState.HTTP_AUTOMATION_LIMITED: "ограничен автоматический HTTP-доступ",
            SourceState.BROWSER_ACCESS_LIMITED: "ограничен доступ из изолированного браузера",
            SourceState.AUTH_REQUIRED: "требуется авторизация на площадке",
            SourceState.UNSUPPORTED_QUERY: "не поддерживает такой запрос",
            SourceState.ROBOTS_RESTRICTED: "ограничен правилами площадки",
            SourceState.PARSE_ERROR: "страница доступна, но формат выдачи не распознан",
            SourceState.BLOCKED: "источник ограничил автоматический доступ",
            SourceState.ERROR: "временно недоступен",
        }.get(state, "работает")

    @staticmethod
    def _display_source(source: str) -> str:
        return {"auto.ru": "Auto.ru", "avito": "Avito", "drom.ru": "Drom"}.get(source, source)
