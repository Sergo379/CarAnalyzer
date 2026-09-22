from typing import Annotated

from fastapi import APIRouter, Depends

from backend.models.car import SearchRequest
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


@router.get("/catalog/regions")
async def catalog_regions() -> dict[str, object]:
    return regions_catalog()
