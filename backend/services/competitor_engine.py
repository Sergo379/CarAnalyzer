from collections.abc import Mapping, Sequence
from enum import StrEnum

from backend.config import Settings, get_settings
from backend.models.car import BodyFilter, BodyType, Car, PriceRanges, SourceVehicle
from backend.models.listing import CarListing, ClassifiedListing, CompetitorGroups


class CompetitorCategory(StrEnum):
    DIRECT = "direct"
    EXPENSIVE = "expensive"
    CHEAPER = "cheaper"


DEFAULT_BODY_COMPATIBILITY: Mapping[BodyType, frozenset[BodyType]] = {
    BodyType.SEDAN: frozenset({BodyType.SEDAN, BodyType.LIFTBACK}),
    BodyType.WAGON: frozenset({BodyType.WAGON}),
    BodyType.HATCHBACK: frozenset({BodyType.HATCHBACK, BodyType.LIFTBACK}),
    BodyType.LIFTBACK: frozenset({BodyType.LIFTBACK, BodyType.SEDAN, BodyType.HATCHBACK}),
    BodyType.COUPE: frozenset({BodyType.COUPE}),
    BodyType.CONVERTIBLE: frozenset({BodyType.CONVERTIBLE}),
    BodyType.SUV: frozenset({BodyType.SUV, BodyType.CROSSOVER}),
    BodyType.CROSSOVER: frozenset({BodyType.CROSSOVER, BodyType.SUV}),
    BodyType.PICKUP: frozenset({BodyType.PICKUP}),
    BodyType.MINIVAN: frozenset({BodyType.MINIVAN}),
    BodyType.VAN: frozenset({BodyType.VAN}),
}


class CompetitorEngine:
    def __init__(
        self,
        settings: Settings | None = None,
        body_compatibility: Mapping[BodyType, frozenset[BodyType]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.body_compatibility = body_compatibility or DEFAULT_BODY_COMPATIBILITY

    def calculate_price_ranges(self, price: int) -> PriceRanges:
        if price <= 0:
            raise ValueError("Price must be positive")
        direct = self.settings.direct_price_percent
        return PriceRanges(
            direct_min=round(price * (1 - direct)),
            direct_max=round(price * (1 + direct)),
            expensive_min=round(price * (1 + direct)) + 1,
            expensive_max=round(price * (1 + self.settings.expensive_max_percent)),
            cheaper_min=round(price * (1 - self.settings.cheaper_max_percent)),
            cheaper_max=round(price * (1 - self.settings.cheaper_min_percent)),
        )

    def body_is_compatible(self, source: BodyType, candidate: BodyType) -> bool:
        return candidate in self.body_compatibility.get(source, frozenset({source}))

    @staticmethod
    def segment_is_compatible(source: str | None, candidate: str | None) -> bool:
        if source is None or candidate is None:
            return True
        return source.strip().casefold() == candidate.strip().casefold()

    @staticmethod
    def normalized_segment_compatible(source: str, candidate: str) -> bool:
        # Unknown data falls back to the existing family/body/price rules.
        return source == "UNKNOWN" or candidate == "UNKNOWN" or source == candidate

    def classify_price(self, source_price: int, candidate_price: int) -> CompetitorCategory | None:
        ranges = self.calculate_price_ranges(source_price)
        if ranges.direct_min <= candidate_price <= ranges.direct_max:
            return CompetitorCategory.DIRECT
        if ranges.expensive_min <= candidate_price <= ranges.expensive_max:
            return CompetitorCategory.EXPENSIVE
        if ranges.cheaper_min <= candidate_price <= ranges.cheaper_max:
            return CompetitorCategory.CHEAPER
        return None

    def classify(
        self, source: Car | SourceVehicle, listings: Sequence[CarListing]
    ) -> CompetitorGroups:
        groups = CompetitorGroups()
        reference_price = (
            source.reference_price if isinstance(source, SourceVehicle) else source.price
        )
        for listing in listings:
            body_matches = source.body_type == BodyFilter.ANY or self.body_is_compatible(
                BodyType(source.body_type.value), listing.body_type
            )
            if not body_matches or not self.segment_is_compatible(source.segment, listing.segment):
                continue
            if not self.normalized_segment_compatible(source.segment_code, listing.segment_code):
                continue
            category = self.classify_price(reference_price, listing.price)
            if category is None:
                continue
            difference = listing.price - reference_price
            item = ClassifiedListing(
                listing=listing,
                price_difference=difference,
                price_difference_percent=round(difference / reference_price * 100, 2),
            )
            getattr(groups, category.value).append(item)

        def rank(item: ClassifiedListing) -> tuple[int, int, int, int]:
            listing = item.listing
            return (
                0
                if source.segment_code != "UNKNOWN" and source.segment_code == listing.segment_code
                else 1,
                0
                if source.market_position != "unknown"
                and source.market_position == listing.market_position
                else 1,
                abs(item.price_difference),
                abs(source.reference_year - listing.year)
                if isinstance(source, SourceVehicle)
                else abs(source.year - listing.year),
            )

        for category in (groups.direct, groups.expensive, groups.cheaper):
            category.sort(key=rank)
        return groups
