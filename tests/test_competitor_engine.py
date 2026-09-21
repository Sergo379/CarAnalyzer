from datetime import UTC, datetime

from backend.config import Settings
from backend.models.car import BodyType, Car
from backend.models.listing import CarListing
from backend.services.competitor_engine import CompetitorCategory, CompetitorEngine


def engine() -> CompetitorEngine:
    return CompetitorEngine(settings=Settings(_env_file=None))


def test_price_ranges_and_categories() -> None:
    service = engine()
    ranges = service.calculate_price_ranges(4_100_000)
    assert (ranges.direct_min, ranges.direct_max) == (3_813_000, 4_387_000)
    assert service.classify_price(4_100_000, 3_900_000) == CompetitorCategory.DIRECT
    assert service.classify_price(4_100_000, 4_700_000) == CompetitorCategory.EXPENSIVE
    assert service.classify_price(4_100_000, 3_500_000) == CompetitorCategory.CHEAPER
    assert service.classify_price(4_100_000, 2_000_000) is None


def test_body_compatibility_is_configured() -> None:
    service = engine()
    assert service.body_is_compatible(BodyType.SEDAN, BodyType.LIFTBACK)
    assert not service.body_is_compatible(BodyType.SEDAN, BodyType.SUV)
    assert not service.body_is_compatible(BodyType.SEDAN, BodyType.WAGON)


def test_classification_computes_difference() -> None:
    source = Car(brand="BMW", model="520i", year=2022, body_type="sedan", price=4_100_000)
    listing = CarListing(
        source="auto.ru",
        external_id="1",
        brand="Audi",
        model="A6",
        year=2022,
        body_type="sedan",
        price=3_900_000,
        url="https://auto.ru/cars/used/sale/1/",
        checked_at=datetime.now(UTC),
    )
    result = engine().classify(source, [listing])
    assert result.direct[0].price_difference == -200_000
    assert result.direct[0].price_difference_percent == -4.88
