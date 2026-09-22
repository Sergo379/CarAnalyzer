import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.models.car import SearchRequest
from backend.services.competitor_engine import CompetitorEngine
from backend.services.marketplace_catalog import CatalogCache
from backend.services.search_service import SearchService


def test_exact_and_range_requests_have_stable_reference_values() -> None:
    exact = SearchRequest(brand="Example", model="One", year=2020, price=1_000_000)
    assert (exact.effective_year_from, exact.effective_year_to) == (2020, 2020)
    assert exact.reference_price == 1_000_000

    ranged = SearchRequest(
        brand="Example",
        model="One",
        year_mode="range",
        year_from=2018,
        year_to=2022,
        price_mode="range",
        price_from=800_000,
        price_to=1_200_000,
    )
    assert ranged.reference_year == 2020
    assert ranged.reference_price == 1_000_000
    assert SearchService._target_price_matches(800_000, ranged)
    assert not SearchService._target_price_matches(799_999, ranged)
    assert CompetitorEngine().calculate_price_ranges(ranged.reference_price).direct_min == 930_000


@pytest.mark.parametrize(
    "values",
    [
        {"year_mode": "range", "year_from": 2022, "year_to": 2020, "price": 1},
        {"year": 2020, "price_mode": "range", "price_from": 2, "price_to": 1},
    ],
)
def test_invalid_ranges_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(brand="Example", model="One", **values)


def test_catalog_generation_range_uses_interval_intersection() -> None:
    path = Path("data/.pytest-range-catalog.json")
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "brands": [
                    {
                        "id": "example",
                        "name": "Example",
                        "models": [
                            {
                                "id": "example:one",
                                "name": "One",
                                "generations": [
                                    {
                                        "id": "old",
                                        "name": "Old",
                                        "year_from": 2010,
                                        "year_to": 2018,
                                    },
                                    {
                                        "id": "new",
                                        "name": "New",
                                        "year_from": 2018,
                                        "year_to": None,
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    cache = CatalogCache(path)
    assert {item["id"] for item in cache.generations("Example", "One", year=2017)} == {"old"}
    assert {
        item["id"] for item in cache.generations("Example", "One", year_from=2018, year_to=2020)
    } == {"old", "new"}
    path.unlink()
