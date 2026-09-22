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
from backend.services.regions import location_matches, region_entry, region_value

logger = logging.getLogger(__name__)


class SourceState(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
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
    source_operation_diagnostics: dict[str, dict[str, SourceDiagnostics]] = Field(
        default_factory=dict
    )
    source_operations: dict[str, dict[str, "SourceOperationResult"]] = Field(default_factory=dict)
    pipeline_diagnostics: dict[str, int] = Field(default_factory=dict)
    direct: CategoryResult
    expensive: CategoryResult
    cheaper: CategoryResult
    car_knowledge: dict[str, dict[str, object]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class MarketplaceOutcome:
    discovered: list[CarListing] | BaseException
    source_listings: list[CarListing] | BaseException


class SourceOperationResult(BaseModel):
    state: SourceState
    count: int = Field(ge=0)
    detail: str
    elapsed_seconds: float = Field(ge=0)


class InvalidSearchSelection(ValueError):
    """A generation/engine selection does not belong to the active model/year window."""


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
        if region_entry(request.region) is None:
            raise InvalidSearchSelection("Selected region is not present in the region catalog")
        identity = self.marketplace_catalog.resolve_identity(request.brand, request.model)
        selected_generation = self.marketplace_catalog.generation(
            identity.brand, identity.model, request.generation_id
        )
        if request.generation_id and selected_generation is None:
            raise InvalidSearchSelection("Selected generation does not belong to this model")
        effective_year_from = request.effective_year_from
        effective_year_to = request.effective_year_to
        if selected_generation is not None:
            if selected_generation.get("year_from") is not None:
                effective_year_from = max(
                    effective_year_from, int(selected_generation["year_from"])
                )
            if selected_generation.get("year_to") is not None:
                effective_year_to = min(effective_year_to, int(selected_generation["year_to"]))
            if effective_year_from > effective_year_to:
                raise InvalidSearchSelection(
                    "Selected generation does not intersect the requested year range"
                )
        selected_engine = self.marketplace_catalog.engine_option(
            identity.brand,
            identity.model,
            request.modification_id,
            generation_id=request.generation_id,
            year_from=effective_year_from,
            year_to=effective_year_to,
        )
        if request.modification_id and selected_engine is None:
            raise InvalidSearchSelection(
                "Selected engine does not belong to this model/generation/year range"
            )
        modification = (
            str(selected_engine["label"])
            if selected_engine
            else request.modification
            or (
                request.model
                if re.fullmatch(r"[1-9]\d{2}[a-z]*", request.model.casefold())
                and not identity.provisional
                else None
            )
        )
        normalized = SearchRequest(
            brand=identity.brand,
            model=identity.model,
            modification=modification,
            year_mode=request.year_mode,
            year=effective_year_from if request.year_mode.value == "exact" else None,
            year_from=effective_year_from if request.year_mode.value == "range" else None,
            year_to=effective_year_to if request.year_mode.value == "range" else None,
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
        source_segment = self._classify_segment(
            normalized.brand,
            normalized.model,
            normalized.reference_year,
            concrete_body,
        )
        generation = str(selected_generation["name"]) if selected_generation else None
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
        operation_diagnostics: dict[str, dict[str, SourceDiagnostics]] = {}
        source_operations: dict[str, dict[str, SourceOperationResult]] = {}
        for scraper, outcome in zip(self.scrapers, outcomes, strict=True):
            scraper_operation_diagnostics = {
                name: value.model_copy(deep=True)
                for name, value in getattr(scraper, "diagnostics", {}).items()
            }
            source_operations[scraper.source] = {
                "target": self._operation_result(
                    outcome.source_listings,
                    scraper_operation_diagnostics.get("target"),
                ),
                "competitors": self._operation_result(
                    outcome.discovered,
                    scraper_operation_diagnostics.get("competitors"),
                ),
            }
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
                combined = scraper.combined_diagnostics()
                if errors or combined.degraded:
                    statuses[scraper.source] = SourceState.PARTIAL
                    details[scraper.source] = (
                        "; ".join(combined.notes) or "Источник работает частично"
                    )
                    warnings.append(
                        f"{self._display_source(scraper.source)} — источник работает частично"
                    )
                else:
                    statuses[scraper.source] = SourceState.OK if count else SourceState.EMPTY
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
            operation_diagnostics[scraper.source] = scraper_operation_diagnostics
            logger.info(
                "marketplace source=%s state=%s discovered=%s target=%s errors=%s",
                scraper.source,
                statuses[scraper.source],
                len(outcome.discovered) if isinstance(outcome.discovered, list) else 0,
                len(outcome.source_listings) if isinstance(outcome.source_listings, list) else 0,
                ",".join(type(error).__name__ for error in errors) or "none",
            )
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
        if source.segment is None:
            observed_segments = {
                self._classify_segment(
                    listing.brand,
                    listing.model,
                    listing.year,
                    listing.body_type,
                )
                for listing in source_unique
            }
            observed_segments.discard(None)
            if len(observed_segments) == 1:
                source.segment = next(iter(observed_segments))

        deduplicated_discovered = deduplicate_listings(discovered)
        after_source_exclusion = [
            listing
            for listing in deduplicated_discovered
            if listing.canonical_model_id != source.canonical_model_id
        ]
        after_transmission = [
            listing
            for listing in after_source_exclusion
            if normalized.transmission == Transmission.ANY
            or listing.transmission == normalized.transmission
        ]
        after_region = [
            listing for listing in after_transmission if self._region_matches(listing, normalized)
        ]
        after_body = [
            listing
            for listing in after_region
            if normalized.body_type == BodyFilter.ANY
            or self.engine.body_is_compatible(
                BodyType(normalized.body_type.value), listing.body_type
            )
        ]
        for listing in after_body:
            listing.segment = self._classify_segment(
                listing.brand,
                listing.model,
                listing.year,
                listing.body_type,
            )
        after_segment = [
            listing
            for listing in after_body
            if self.engine.segment_is_compatible(source.segment, listing.segment)
        ]
        after_price = [
            listing
            for listing in after_segment
            if self.engine.classify_price(source.reference_price, listing.price) is not None
        ]
        groups = self.engine.classify(source, after_price)
        pipeline_diagnostics = {
            "discovered_total": len(discovered),
            "after_dedup": len(deduplicated_discovered),
            "after_source_exclusion": len(after_source_exclusion),
            "after_transmission": len(after_transmission),
            "after_region": len(after_region),
            "after_body": len(after_body),
            "after_segment": len(after_segment),
            "after_price": len(after_price),
            "direct": len(groups.direct),
            "expensive": len(groups.expensive),
            "cheaper": len(groups.cheaper),
        }
        categorized = groups.direct + groups.expensive + groups.cheaper
        knowledge: dict[str, dict[str, object]] = {}
        vehicles = [item.listing for item in categorized]
        source_key = source.canonical_model_id
        knowledge[source_key] = self.knowledge.get(
            source.brand,
            source.model,
            source.reference_year,
            concrete_body,
            source.segment,
            generation=source.generation or "",
        ).model_dump(mode="json")
        for vehicle in vehicles:
            key = vehicle.canonical_model_id or f"{vehicle.brand}|{vehicle.model}"
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
            source_operation_diagnostics=operation_diagnostics if request.debug else {},
            source_operations=source_operations,
            pipeline_diagnostics=pipeline_diagnostics if request.debug else {},
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
        async def measured(operation: str, coroutine):
            operation_started = time.perf_counter()
            try:
                budget = (
                    self._settings().target_operation_timeout_seconds
                    if operation == "target"
                    else self._settings().discovery_operation_timeout_seconds
                )
                budget = min(budget, self._settings().source_search_timeout_seconds)
                return await asyncio.wait_for(coroutine, timeout=budget)
            finally:
                diagnostic = getattr(scraper, "diagnostics", {}).get(operation)
                if diagnostic is not None:
                    diagnostic.elapsed_seconds = time.perf_counter() - operation_started

        tasks = {
            "discovered": asyncio.create_task(measured("competitors", scraper.discover(discovery))),
            "source_listings": asyncio.create_task(
                measured("target", scraper.search(source_query))
            ),
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

    def _operation_result(
        self,
        value: list[CarListing] | BaseException,
        diagnostic: SourceDiagnostics | None,
    ) -> SourceOperationResult:
        elapsed = diagnostic.elapsed_seconds if diagnostic is not None else 0
        if isinstance(value, list):
            state = SourceState.OK if value else SourceState.EMPTY
            if diagnostic is not None and diagnostic.degraded:
                state = SourceState.PARTIAL
            detail = (
                "; ".join(diagnostic.notes)
                if diagnostic is not None and diagnostic.notes
                else "Работает"
                if value
                else "Подходящих объявлений нет"
            )
            return SourceOperationResult(
                state=state,
                count=len(value),
                detail=detail,
                elapsed_seconds=elapsed,
            )
        state = self._state_for_error(value)
        return SourceOperationResult(
            state=state,
            count=0,
            detail=self._message_for_state(state),
            elapsed_seconds=elapsed,
        )

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
            guaranteed = listing.raw_metadata.get(
                "region_filter_guaranteed"
            ) is True and listing.raw_metadata.get("region_scope") == region_value(query.region)
            if not guaranteed:
                return "region"
        if (
            query.generation_id
            and listing.canonical_generation_id
            and listing.canonical_generation_id != query.generation_id
        ):
            return "generation"
        if query.modification_id and not self._engine_matches_listing(listing, query):
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
        if listing.raw_metadata.get(
            "region_filter_guaranteed"
        ) is True and listing.raw_metadata.get("region_scope") == region_value(query.region):
            return True
        return location_matches(query.region, listing.location, listing.city, listing.region)

    @staticmethod
    def _region_matches(listing: CarListing, query: SearchRequest) -> bool:
        if listing.raw_metadata.get(
            "region_filter_guaranteed"
        ) is True and listing.raw_metadata.get("region_scope") == region_value(query.region):
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

    def _engine_matches_listing(self, listing: CarListing, query: SearchRequest) -> bool:
        option = self.marketplace_catalog.engine_option(
            query.brand,
            query.model,
            query.modification_id,
            generation_id=query.generation_id,
            year_from=query.effective_year_from,
            year_to=query.effective_year_to,
        )
        if option is None:
            return False
        engine = option.get("engine") or {}
        checks = (
            ("fuel_type", listing.fuel_type),
            ("displacement_l", listing.engine_displacement),
            ("power_hp", listing.power_hp),
            ("engine_code", listing.engine_code),
        )
        compared = False
        for field, actual in checks:
            expected = engine.get(field)
            if expected is None:
                continue
            compared = True
            if actual is None or str(actual).casefold() != str(expected).casefold():
                return False
        return compared

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
        body_type: BodyType | None,
    ) -> str | None:
        try:
            entry = self.catalog.classify(brand, model, year, body_type)
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
            SourceState.PARTIAL: "источник работает частично",
            SourceState.TIMEOUT: "превышен лимит времени источника",
            SourceState.ERROR: "временно недоступен",
        }.get(state, "работает")

    @staticmethod
    def _display_source(source: str) -> str:
        return {"auto.ru": "Auto.ru", "avito": "Avito", "drom.ru": "Drom"}.get(source, source)
