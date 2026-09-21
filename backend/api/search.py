from fastapi import APIRouter
from pydantic import BaseModel

from backend.models.car import PriceRanges, SearchRequest
from backend.models.listing import CompetitorGroups
from backend.services.competitor_engine import CompetitorEngine
from backend.services.normalizer import Normalizer

router = APIRouter(prefix="/api", tags=["search"])


class SearchResponse(BaseModel):
    query: SearchRequest
    price_ranges: PriceRanges
    competitors: CompetitorGroups
    source_status: str


@router.post("/search", response_model=SearchResponse)
async def search(request: SearchRequest) -> SearchResponse:
    normalized = SearchRequest(
        brand=Normalizer.brand(request.brand),
        model=Normalizer.model(request.model),
        year=request.year,
        body_type=request.body_type,
        price=request.price,
    )
    engine = CompetitorEngine()
    return SearchResponse(
        query=normalized,
        price_ranges=engine.calculate_price_ranges(normalized.price),
        competitors=CompetitorGroups(),
        source_status="foundation_ready_auto_ru_not_connected",
    )
