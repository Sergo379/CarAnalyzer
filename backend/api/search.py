import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from backend.models.car import SearchRequest
from backend.services.marketplace_catalog import CatalogCache, CatalogEnrichmentService
from backend.services.reference_data import regions_catalog, vehicle_catalog
from backend.services.search_service import InvalidSearchSelection, SearchResult, SearchService

router = APIRouter(prefix="/api", tags=["search"])
logger = logging.getLogger(__name__)


def get_search_service() -> SearchService:
    return SearchService()


@router.post("/search", response_model=SearchResult)
async def search(
    request: SearchRequest,
    service: Annotated[SearchService, Depends(get_search_service)],
) -> SearchResult:
    try:
        return await service.search(request)
    except InvalidSearchSelection as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/catalog/vehicles")
async def catalog_vehicles() -> dict[str, object]:
    return vehicle_catalog()


@router.get("/catalog/brands")
async def catalog_brands() -> list[str]:
    return CatalogCache().brands()


@router.get("/catalog/models")
async def catalog_models(brand: str) -> list[str]:
    return CatalogCache().models(brand)


def _year_window(year: int | None, year_from: int | None, year_to: int | None) -> tuple[int, int]:
    if year is not None:
        return year, year
    if year_from is None or year_to is None or year_from > year_to:
        raise HTTPException(422, "Provide year or a valid year_from/year_to range")
    return year_from, year_to


async def _enrich_window(cache: CatalogCache, brand: str, model: str, start: int, end: int) -> str:
    try:
        state = await CatalogEnrichmentService(cache).ensure_window(brand, model, start, end)
        if state not in {"fresh", "cached", "complete", "source_has_no_generation_data"}:
            logger.warning("catalog enrichment state=%s brand=%s model=%s", state, brand, model)
        return state
    except Exception as exc:
        logger.warning(
            "catalog enrichment state=source_unavailable brand=%s model=%s error_type=%s",
            brand,
            model,
            type(exc).__name__,
        )
        return "source_unavailable"


def _generation_options(
    cache: CatalogCache, brand: str, model: str, start: int, end: int
) -> list[dict[str, object]]:
    return [
        {
            "id": item["id"],
            "label": item["name"],
            "name": item["name"],
            "year_from": item.get("year_from"),
            "year_to": item.get("year_to"),
        }
        for item in cache.generations(brand, model, year_from=start, year_to=end)
    ]


@router.get("/catalog/generations")
async def catalog_generations(
    brand: str,
    model: str,
    year: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[dict[str, object]]:
    cache = CatalogCache()
    start, end = _year_window(year, year_from, year_to)
    await _enrich_window(cache, brand, model, start, end)
    return _generation_options(cache, brand, model, start, end)


@router.get("/catalog/generation-state")
async def catalog_generation_state(
    brand: str,
    model: str,
    year: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
) -> dict[str, object]:
    cache = CatalogCache()
    start, end = _year_window(year, year_from, year_to)
    state = await _enrich_window(cache, brand, model, start, end)
    options = _generation_options(cache, brand, model, start, end)
    return {
        "status": "ready" if state in {"complete", "fresh", "cached"} else state,
        "generations": options,
    }


@router.get("/catalog/engines")
async def catalog_engines(
    brand: str,
    model: str,
    year: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    generation_id: str | None = None,
) -> list[dict[str, object]]:
    cache = CatalogCache()
    start, end = _year_window(year, year_from, year_to)
    await _enrich_window(cache, brand, model, start, end)
    return cache.engine_options(
        brand,
        model,
        generation_id=generation_id,
        year_from=start,
        year_to=end,
    )


@router.get("/catalog/status")
async def catalog_status() -> dict[str, object]:
    return CatalogCache().status()


@router.get("/catalog/regions")
async def catalog_regions() -> dict[str, object]:
    return regions_catalog()
