from contextlib import suppress
from typing import Annotated

from fastapi import APIRouter, Depends

from backend.models.car import SearchRequest
from backend.models.catalog import VehicleModification
from backend.services.marketplace_catalog import CatalogCache, CatalogEnrichmentService
from backend.services.reference_data import regions_catalog, vehicle_catalog
from backend.services.search_service import SearchResult, SearchService

router = APIRouter(prefix="/api", tags=["search"])


def get_search_service() -> SearchService:
    return SearchService()


@router.post("/search", response_model=SearchResult)
async def search(
    request: SearchRequest,
    service: Annotated[SearchService, Depends(get_search_service)],
) -> SearchResult:
    return await service.search(request)


@router.get("/catalog/vehicles")
async def catalog_vehicles() -> dict[str, object]:
    return vehicle_catalog()


@router.get("/catalog/brands")
async def catalog_brands() -> list[str]:
    return CatalogCache().brands()


@router.get("/catalog/models")
async def catalog_models(brand: str) -> list[str]:
    return CatalogCache().models(brand)


@router.get("/catalog/generations")
async def catalog_generations(brand: str, model: str, year: int) -> list[dict[str, object]]:
    cache = CatalogCache()
    with suppress(Exception):
        await CatalogEnrichmentService(cache).ensure(brand, model, year)
    return [
        {
            "id": item["id"],
            "label": item["name"],
            "name": item["name"],
            "year_from": item.get("year_from"),
            "year_to": item.get("year_to"),
        }
        for item in cache.generations(brand, model, year)
    ]


@router.get("/catalog/engines")
async def catalog_engines(
    brand: str,
    model: str,
    year: int,
    generation_id: str | None = None,
) -> list[dict[str, object]]:
    cache = CatalogCache()
    with suppress(Exception):
        await CatalogEnrichmentService(cache).ensure(brand, model, year)
    return [
        {
            **item,
            "label": VehicleModification.model_validate(item).label,
        }
        for item in cache.modifications(brand, model, year, generation_id)
    ]


@router.get("/catalog/status")
async def catalog_status() -> dict[str, object]:
    return CatalogCache().status()


@router.get("/catalog/regions")
async def catalog_regions() -> dict[str, object]:
    return regions_catalog()
