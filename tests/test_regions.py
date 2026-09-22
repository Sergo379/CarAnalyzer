import pytest

from backend.models.car import SearchRegion
from backend.services.regions import location_matches


@pytest.mark.parametrize("location", ["Москва", "Химки", "Московская область"])
def test_moscow_and_oblast_accepts_only_its_supported_area(location: str) -> None:
    assert location_matches(SearchRegion.MOSCOW_AND_OBLAST, location)


@pytest.mark.parametrize("location", ["Владивосток", "Владимирская область", "Тула"])
def test_moscow_and_oblast_rejects_other_regions(location: str) -> None:
    assert not location_matches(SearchRegion.MOSCOW_AND_OBLAST, location)


def test_moscow_oblast_excludes_moscow_city() -> None:
    assert location_matches(SearchRegion.MOSCOW_OBLAST, "Химки")
    assert not location_matches(SearchRegion.MOSCOW_OBLAST, "Москва")
