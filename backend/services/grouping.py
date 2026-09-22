from collections import defaultdict
from collections.abc import Iterable

from backend.models.listing import ClassifiedListing, ModelGroup


def group_by_model(items: Iterable[ClassifiedListing]) -> list[ModelGroup]:
    grouped: dict[tuple[str, str], list[ClassifiedListing]] = defaultdict(list)
    for item in items:
        key = (
            item.listing.canonical_brand_id or item.listing.brand.casefold(),
            item.listing.canonical_model_id or item.listing.model.casefold(),
        )
        grouped[key].append(item)

    result: list[ModelGroup] = []
    for values in grouped.values():
        prices = [value.listing.price for value in values]
        first = values[0].listing
        result.append(
            ModelGroup(
                brand=first.brand,
                model=first.model,
                listings_count=len(values),
                average_price=round(sum(prices) / len(prices)),
                min_price=min(prices),
                max_price=max(prices),
            )
        )
    return sorted(result, key=lambda group: (group.brand.casefold(), group.model.casefold()))
