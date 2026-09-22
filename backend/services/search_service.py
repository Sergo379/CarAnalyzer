import asyncio
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from backend.config import Settings, get_settings
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
    SourceDiagnostics,
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
    TIMEOUT = "timeout"
    ERROR = "error"


class SearchResult(BaseModel):
    source_vehicle: SourceVehicle
    source_status: dict[str, SourceState]
    source_details: dict[str, str] = Field(default_factory=dict)
    source_listings: CategoryResult = Field(default_factory=CategoryResult)
    source_model_group: ModelGroup | None = None
    source_distribution: dict[str, int] = Field(default_factory=dict)
    source_diagnostics: dict[str, SourceDiagnostics] = Field(default_factory=dict)
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
        started = time.perf_counter()
        identity = self.marketplace_catalog.resolve_identity(request.brand, request.model)
        selected_modification = self.marketplace_catalog.modification(request.modification_id)
        exact_catalog_model = self.marketplace_catalog.model_entry(request.brand, request.model)
        modification = (
            str(selected_modification["name"])
            if selected_modification
            else request.modification
            or (request.model if exact_catalog_model is None and not identity.provisional else None)
        )
        normalized = SearchRequest(
            brand=identity.brand,
            model=identity.model,
            modification=modification,
            year_mode=request.year_mode,
            year=request.year,
            year_from=request.year_from,
            year_to=request.year_to,
            body_type=request.body_type,
            transmission=request.transmission,
            region=request.region,
            price_mode=request.price_mode,
            price=request.price,
            price_from=request.price_from,
            price_to=request.price_to,
            generation_id=request.generation_id,
            modification_id=request.modification_id,
            debug=request.debug,
        )
        concrete_body = (
            None if normalized.body_type == BodyFilter.ANY else BodyType(normalized.body_type.value)
        )
        source_segment = (
            self._classify_segment(
                normalized.brand,
                normalized.model,
                normalized.reference_year,
                concrete_body,
                normalized.reference_price,
            )
            if concrete_body
            else None
        )
        generation = next(
            (
                str(item["name"])
                for item in self.marketplace_catalog.generations(
                    normalized.brand,
                    normalized.model,
                    year_from=normalized.effective_year_from,
                    year_to=normalized.effective_year_to,
                )
                if item["id"] == normalized.generation_id
            ),
            None,
        )
        source = SourceVehicle(
            **normalized.model_dump(),
            segment=source_segment,
            generation=generation,
            canonical_brand_id=identity.canonical_brand_id,
            canonical_model_id=identity.canonical_model_id,
            canonical_generation_id=normalized.generation_id,
            canonical_modification_id=normalized.modification_id,
        )
        ranges = self.engine.calculate_price_ranges(normalized.reference_price)
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

        tasks = [
            asyncio.create_task(self._search_marketplace(scraper, discovery, normalized))
            for scraper in self.scrapers
        ]
        if tasks:
            done, pending = await asyncio.wait(
                tasks, timeout=self._settings().search_total_timeout_seconds
            )
        else:
            done, pending = set(), set()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        outcomes: list[MarketplaceOutcome] = []
        for task in tasks:
            if task not in done:
                outcomes.append(MarketplaceOutcome(TimeoutError(), TimeoutError()))
                continue
            try:
                outcomes.append(task.result())
            except BaseException as exc:
                outcomes.append(MarketplaceOutcome(exc, exc))
        statuses: dict[str, SourceState] = {}
        details: dict[str, str] = {}
        warnings: list[str] = []
        discovered: list[CarListing] = []
        target_listings: list[CarListing] = []
        source_distribution: dict[str, int] = {}
        diagnostics: dict[str, SourceDiagnostics] = {}
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
                    warnings.append(
                        f"{self._display_source(scraper.source)} — источник работает частично"
                    )
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
                source_distribution[scraper.source] = len(outcome.source_listings)
            else:
                source_distribution[scraper.source] = 0
            diagnostics[scraper.source] = scraper.combined_diagnostics()
            logger.info(
                "marketplace source=%s state=%s discovered=%s target=%s errors=%s",
                scraper.source,
                statuses[scraper.source],
                len(outcome.discovered) if isinstance(outcome.discovered, list) else 0,
                len(outcome.source_listings) if isinstance(outcome.source_listings, list) else 0,
                ",".join(type(error).__name__ for error in errors) or "none",
            )
        for value in diagnostics.values():
            value.elapsed_seconds = max(value.elapsed_seconds, time.perf_counter() - started)

        for listing in target_listings + discovered:
            self._attach_canonical_identity(listing)

        deduplicated_targets = deduplicate_listings(target_listings)
        duplicate_counts: dict[str, int] = {}
        for listing in target_listings:
            duplicate_counts[listing.source] = duplicate_counts.get(listing.source, 0) + 1
        for listing in deduplicated_targets:
            duplicate_counts[listing.source] -= 1
        source_unique: list[CarListing] = []
        for listing in deduplicated_targets:
            reason = self._source_rejection_reason(listing, normalized)
            source_diagnostic = diagnostics.setdefault(listing.source, SourceDiagnostics())
            if reason:
                source_diagnostic.rejected[reason] = source_diagnostic.rejected.get(reason, 0) + 1
            else:
                source_unique.append(listing)
        for source_name, count in duplicate_counts.items():
            if count:
                source_diagnostic = diagnostics.setdefault(source_name, SourceDiagnostics())
                source_diagnostic.rejected["duplicate"] = (
                    source_diagnostic.rejected.get("duplicate", 0) + count
                )
        source_distribution = {scraper.source: 0 for scraper in self.scrapers}
        for item in source_unique:
            source_distribution[item.source] = source_distribution.get(item.source, 0) + 1
        source_items = [
            self._classified_item(source.reference_price, listing) for listing in source_unique
        ]

        competitors = []
        for listing in deduplicate_listings(discovered):
            if listing.canonical_model_id == source.canonical_model_id:
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
        source_identity_parts = [source.canonical_model_id]
        if source.canonical_generation_id:
            source_identity_parts.append(source.canonical_generation_id)
        if source.canonical_modification_id:
            source_identity_parts.append(source.canonical_modification_id)
        source_identity_parts.append(str(source.reference_year))
        source_key = "|".join(source_identity_parts)
        knowledge[source_key] = self.knowledge.get(
            source.brand,
            source.model,
            source.reference_year,
            concrete_body,
            source.segment,
            generation=source.generation or "",
        ).model_dump(mode="json")
        for vehicle in vehicles:
            vehicle_key = vehicle.canonical_model_id or f"{vehicle.brand}|{vehicle.model}"
            vehicle_identity_parts = [vehicle_key]
            if vehicle.canonical_generation_id:
                vehicle_identity_parts.append(vehicle.canonical_generation_id)
            if vehicle.canonical_modification_id:
                vehicle_identity_parts.append(vehicle.canonical_modification_id)
            vehicle_identity_parts.append(str(vehicle.year))
            key = "|".join(vehicle_identity_parts)
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
            source_distribution=source_distribution,
            source_diagnostics=diagnostics if request.debug else {},
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

    async def _search_marketplace(
        self,
        scraper: BaseScraper,
        discovery: MarketDiscoveryRequest,
        source_query: SearchRequest,
    ) -> MarketplaceOutcome:
        tasks = {
            "discovered": asyncio.create_task(scraper.discover(discovery)),
            "source_listings": asyncio.create_task(scraper.search(source_query)),
        }
        done, pending = await asyncio.wait(
            tasks.values(), timeout=self._settings().source_search_timeout_seconds
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        values: dict[str, list[CarListing] | BaseException] = {}
        for name, task in tasks.items():
            if task not in done:
                values[name] = TimeoutError()
            else:
                try:
                    values[name] = task.result()
                except BaseException as exc:
                    values[name] = exc
        discovered = values["discovered"]
        source_listings = values["source_listings"]
        return MarketplaceOutcome(discovered=discovered, source_listings=source_listings)

    def _settings(self) -> Settings:
        if self.scrapers:
            value = getattr(self.scrapers[0], "settings", None)
            if isinstance(value, Settings):
                return value
        return get_settings()

    def _matches_source(self, listing: CarListing, query: SearchRequest) -> bool:
        return self._source_rejection_reason(listing, query) is None

    def _source_rejection_reason(self, listing: CarListing, query: SearchRequest) -> str | None:
        query_identity = self.marketplace_catalog.resolve_identity(query.brand, query.model)
        if listing.canonical_model_id != query_identity.canonical_model_id:
            return "model"
        if not query.effective_year_from <= listing.year <= query.effective_year_to:
            return "year"
        if not self._target_price_matches(listing.price, query):
            return "price"
        if query.body_type != BodyFilter.ANY and listing.body_type.value != query.body_type.value:
            return "body"
        if query.transmission != Transmission.ANY and listing.transmission != query.transmission:
            return "transmission"
        if not location_matches(query.region, listing.location, listing.city, listing.region):
            guaranteed = (
                listing.raw_metadata.get("region_filter_guaranteed") is True
                and listing.raw_metadata.get("region_scope") == query.region.value
            )
            if not guaranteed:
                return "region"
        if (
            query.generation_id
            and listing.canonical_generation_id
            and listing.canonical_generation_id != query.generation_id
        ):
            return "generation"
        if (
            query.modification_id
            and listing.modification
            and not self._same_modification(listing.modification, query.modification or "")
        ):
            return "engine"
        return None

    @staticmethod
    def _target_price_matches(price: int, query: SearchRequest) -> bool:
        if query.price_mode.value == "range":
            return query.effective_price_from <= price <= query.effective_price_to
        return round(query.reference_price * 0.8) <= price <= round(query.reference_price * 1.2)

    def _attach_canonical_identity(self, listing: CarListing) -> None:
        identity = self.marketplace_catalog.resolve_identity(listing.brand, listing.model)
        listing.canonical_brand_id = identity.canonical_brand_id
        listing.canonical_model_id = identity.canonical_model_id
        for generation in self.marketplace_catalog.generations(
            identity.brand, identity.model, listing.year
        ):
            if listing.generation and self._same_modification(
                listing.generation, str(generation["name"])
            ):
                listing.canonical_generation_id = str(generation["id"])
            if not listing.modification:
                continue
            for modification in generation.get("modifications", []):
                if self._same_modification(listing.modification, str(modification.get("name", ""))):
                    listing.canonical_generation_id = str(generation["id"])
                    listing.canonical_modification_id = str(modification["id"])
                    return

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

        return compact(brand_a) == compact(brand_b) and compact(model_a) == compact(model_b)

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
        if isinstance(error, TimeoutError):
            return SourceState.TIMEOUT
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
            SourceState.TIMEOUT: "превышен лимит времени источника",
            SourceState.ERROR: "временно недоступен",
        }.get(state, "работает")

    @staticmethod
    def _display_source(source: str) -> str:
        return {"auto.ru": "Auto.ru", "avito": "Avito", "drom.ru": "Drom"}.get(source, source)
