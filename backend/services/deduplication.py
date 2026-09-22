from collections.abc import Iterable

from backend.models.listing import CarListing


def deduplicate_listings(listings: Iterable[CarListing]) -> list[CarListing]:
    """Remove only proven duplicates; equal model/year/price is not sufficient."""
    result: list[CarListing] = []
    seen_source_ids: set[tuple[str, str]] = set()
    seen_vins: set[str] = set()
    for listing in listings:
        source_key = (listing.source.casefold(), listing.external_id)
        if source_key in seen_source_ids:
            continue
        vin_value = listing.raw_metadata.get("vin")
        vin = str(vin_value).strip().upper() if vin_value else ""
        if len(vin) == 17 and "*" not in vin and vin in seen_vins:
            continue
        seen_source_ids.add(source_key)
        if len(vin) == 17 and "*" not in vin:
            seen_vins.add(vin)
        result.append(listing)
    return result
