from datetime import UTC, datetime

from backend.models.listing import CarListing
from backend.services.deduplication import deduplicate_listings


def make_listing(source: str, external_id: str) -> CarListing:
    return CarListing(
        source=source,
        external_id=external_id,
        brand="BMW",
        model="520i",
        year=2022,
        body_type="sedan",
        price=4_100_000,
        url=f"https://example.com/{source}/{external_id}",
        checked_at=datetime.now(UTC),
    )


def test_deduplicates_only_stable_identity() -> None:
    first = make_listing("auto.ru", "1")
    same = make_listing("auto.ru", "1")
    different_source_same_car_fields = make_listing("drom.ru", "1")
    result = deduplicate_listings([first, same, different_source_same_car_fields])
    assert result == [first, different_source_same_car_fields]
