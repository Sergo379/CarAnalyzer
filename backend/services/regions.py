from backend.models.car import SearchRegion
from backend.services.reference_data import regions_catalog


def region_value(region: str | SearchRegion) -> str:
    return region.value if isinstance(region, SearchRegion) else region


def region_entry(region: str | SearchRegion) -> dict[str, object] | None:
    selected = region_value(region)
    return next(
        (item for item in regions_catalog().get("regions", []) if item["value"] == selected),
        None,
    )


def auto_ru_region_prefix(region: str | SearchRegion) -> str:
    entry = region_entry(region)
    path = entry.get("auto_ru_path") if entry else None
    return f"{path.strip('/')}/" if isinstance(path, str) and path else ""


def drom_region_prefix(region: str | SearchRegion) -> str:
    entry = region_entry(region)
    path = entry.get("drom_path") if entry else None
    return f"{path.strip('/')}/" if isinstance(path, str) and path else ""


def drom_region_query(region: str | SearchRegion) -> str:
    entry = region_entry(region)
    value = entry.get("drom_query") if entry else None
    return f"&{value}" if isinstance(value, str) and value else ""


def location_matches(
    selected: str | SearchRegion,
    location: str | None,
    city: str | None = None,
    region: str | None = None,
) -> bool:
    selected_value = region_value(selected)
    if selected_value == SearchRegion.ANY.value:
        return True
    entry = region_entry(selected_value)
    if entry is None:
        return False
    folded = " ".join(value for value in (location, city, region) if value).casefold()
    if not folded:
        return False
    aliases = [str(entry.get("label", "")), *(str(x) for x in entry.get("aliases", []))]
    return any(alias.casefold() in folded for alias in aliases if alias)
