from dataclasses import dataclass

from backend.models.car import BodyType
from backend.services.marketplace_catalog import CatalogCache, get_catalog_cache
from backend.services.normalizer import Normalizer


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    brand: str
    model: str
    segment: str | None
    body_types: frozenset[BodyType]
    category: str | None
    size_class: str | None = None
    positioning: str | None = None
    confidence: str = "unknown"
    provisional: bool = True


class VehicleCatalog:
    """Stable catalog classifier; it never writes to the knowledge database."""

    def __init__(self, cache: CatalogCache | None = None) -> None:
        self.cache = cache or get_catalog_cache()

    def classify(
        self,
        brand: str,
        model: str,
        year: int,
        body_type: BodyType | None,
        market_price: int | None = None,
    ) -> CatalogEntry:
        identity = self.cache.resolve_identity(brand, model)
        model_entry = self.cache.model_entry(identity.brand, identity.model)
        raw = model_entry.get("classification", {}) if model_entry else {}
        bodies = self._catalog_bodies(identity.brand, identity.model, year)
        if body_type is not None:
            bodies.add(body_type)
        categories = {self._category(value) for value in bodies}
        categories.discard(None)
        category = raw.get("category") or (next(iter(categories)) if len(categories) == 1 else None)
        confidence = "catalog" if raw else "body_fallback" if category else "unknown"
        return CatalogEntry(
            brand=identity.brand if not identity.provisional else Normalizer.brand(brand),
            model=identity.model if not identity.provisional else Normalizer.model(model),
            segment=str(category) if category else None,
            body_types=frozenset(bodies),
            category=str(category) if category else None,
            size_class=str(raw["size_class"]) if raw.get("size_class") else None,
            positioning=str(raw["positioning"]) if raw.get("positioning") else None,
            confidence=confidence,
            provisional=confidence != "catalog",
        )

    def _catalog_bodies(self, brand: str, model: str, year: int) -> set[BodyType]:
        return {
            BodyType(str(value))
            for generation in self.cache.generations(brand, model, year)
            for value in generation.get("body_types", [])
        }

    @staticmethod
    def _category(body_type: BodyType) -> str | None:
        if body_type in {
            BodyType.SEDAN,
            BodyType.WAGON,
            BodyType.HATCHBACK,
            BodyType.LIFTBACK,
            BodyType.COUPE,
            BodyType.CONVERTIBLE,
        }:
            return "passenger"
        if body_type in {BodyType.SUV, BodyType.CROSSOVER}:
            return "suv"
        if body_type == BodyType.MINIVAN:
            return "mpv"
        if body_type in {BodyType.VAN, BodyType.PICKUP}:
            return "commercial"
        return None
