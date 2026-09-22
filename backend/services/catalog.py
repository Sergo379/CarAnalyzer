from dataclasses import dataclass

from backend.database.knowledge import KnowledgeRepository
from backend.models.car import BodyType
from backend.services.normalizer import Normalizer


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    brand: str
    model: str
    segment: str
    body_types: frozenset[BodyType]
    provisional: bool = True


class VehicleCatalog:
    """SQLite-backed, self-extending classifier; never a candidate generator."""

    def __init__(self, repository: KnowledgeRepository | None = None) -> None:
        self.repository = repository or KnowledgeRepository()

    def classify(
        self,
        brand: str,
        model: str,
        year: int,
        body_type: BodyType,
        market_price: int,
    ) -> CatalogEntry:
        normalized_brand = Normalizer.brand(brand)
        normalized_model = Normalizer.model(model)
        existing = self.repository.get_profile(normalized_brand, normalized_model, year)
        segment = (
            existing.segment
            if existing is not None and existing.segment
            else self._provisional_segment(body_type, market_price)
        )
        self.repository.upsert_profile(
            brand=normalized_brand,
            model=normalized_model,
            year=year,
            segment=segment,
            body_types=[body_type],
        )
        return CatalogEntry(
            brand=normalized_brand,
            model=normalized_model,
            segment=segment,
            body_types=frozenset({body_type}),
        )

    @staticmethod
    def _provisional_segment(body_type: BodyType, price: int) -> str:
        if body_type in {BodyType.SUV, BodyType.CROSSOVER, BodyType.PICKUP}:
            position = "utility"
        elif body_type in {BodyType.MINIVAN, BodyType.VAN}:
            position = "people_cargo"
        else:
            position = "passenger"
        if price >= 3_000_000:
            tier = "premium"
        elif price >= 1_200_000:
            tier = "mainstream"
        else:
            tier = "value"
        return f"{position}_{tier}"
