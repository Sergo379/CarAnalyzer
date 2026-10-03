"""Catalog-backed technical-knowledge identity and safe hierarchy fallback."""

import json
import unicodedata
from dataclasses import dataclass
from itertools import product

from backend.services.marketplace_catalog import CatalogCache


@dataclass(frozen=True, slots=True)
class KnowledgeIdentity:
    canonical_brand_id: str
    canonical_model_id: str
    canonical_generation_id: str | None = None
    canonical_engine_id: str | None = None
    canonical_modification_id: str | None = None
    brand: str = ""
    model: str = ""
    generation: str = ""
    engine: str = ""
    generation_year_from: int | None = None
    generation_year_to: int | None = None
    requested_year: int | None = None

    def __post_init__(self) -> None:
        for field in (
            "canonical_generation_id",
            "canonical_engine_id",
            "canonical_modification_id",
        ):
            if getattr(self, field) == "":
                object.__setattr__(self, field, None)
        if self.canonical_modification_id and not self.canonical_engine_id:
            raise ValueError("Modification requires a canonical engine")
        if self.canonical_engine_id and not self.canonical_generation_id:
            raise ValueError("Engine requires a canonical generation")

    @property
    def scope_key(self) -> str:
        return json.dumps(
            [
                self.canonical_brand_id,
                self.canonical_model_id,
                self.canonical_generation_id,
                self.canonical_engine_id,
                self.canonical_modification_id,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def fallback(self) -> tuple["KnowledgeIdentity", ...]:
        """Specific → generation → model, never a sibling engine/generation."""
        result = [self]
        if self.canonical_modification_id:
            result.append(self._without_modification())
        if self.canonical_engine_id:
            result.append(self._without_engine())
        if self.canonical_generation_id:
            result.append(self._without_generation())
        return tuple(result)

    def storage_scope_keys(self) -> tuple[str, ...]:
        """Read legacy empty IDs as absent, never alias a different vehicle scope."""
        optional = (
            self.canonical_generation_id,
            self.canonical_engine_id,
            self.canonical_modification_id,
        )
        return tuple(
            json.dumps(
                [self.canonical_brand_id, self.canonical_model_id, *values],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for values in product(
                *[(value,) if value is not None else (None, "") for value in optional]
            )
        )

    def _without_modification(self) -> "KnowledgeIdentity":
        return KnowledgeIdentity(
            self.canonical_brand_id,
            self.canonical_model_id,
            self.canonical_generation_id,
            self.canonical_engine_id,
            brand=self.brand,
            model=self.model,
            generation=self.generation,
            engine=self.engine,
            generation_year_from=self.generation_year_from,
            generation_year_to=self.generation_year_to,
            requested_year=self.requested_year,
        )

    def _without_engine(self) -> "KnowledgeIdentity":
        return KnowledgeIdentity(
            self.canonical_brand_id,
            self.canonical_model_id,
            self.canonical_generation_id,
            brand=self.brand,
            model=self.model,
            generation=self.generation,
            generation_year_from=self.generation_year_from,
            generation_year_to=self.generation_year_to,
            requested_year=self.requested_year,
        )

    def _without_generation(self) -> "KnowledgeIdentity":
        return KnowledgeIdentity(
            self.canonical_brand_id,
            self.canonical_model_id,
            brand=self.brand,
            model=self.model,
        )


def normalize_display_text(value: object) -> str:
    """Keep catalog display metadata Unicode-clean and repair common UTF-8/Latin-1 mojibake."""
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    markers = ("Ã", "Â", "Ð", "Ñ", "â€", "�")
    if not any(marker in text for marker in markers):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    original_score = sum(text.count(marker) for marker in markers)
    repaired_score = sum(repaired.count(marker) for marker in markers)
    return unicodedata.normalize("NFC", repaired) if repaired_score < original_score else text


def resolve_knowledge_identity(
    cache: CatalogCache,
    brand: str,
    model: str,
    generation_id: str | None = None,
    engine_id: str | None = None,
    modification_id: str | None = None,
) -> KnowledgeIdentity:
    generation_id = generation_id or None
    engine_id = engine_id or None
    modification_id = modification_id or None
    canonical = cache.resolve_identity(brand, model)
    if canonical.provisional:
        raise ValueError("Vehicle must resolve to a canonical catalog brand and model")
    generation = cache.generation(canonical.brand, canonical.model, generation_id)
    if generation_id and generation is None:
        raise ValueError("Generation does not belong to the selected model")
    if (engine_id or modification_id) and generation is None:
        raise ValueError("Select a canonical generation before the engine")
    engine = cache.engine_option(
        canonical.brand, canonical.model, engine_id or modification_id, generation_id
    )
    if (engine_id or modification_id) and engine is None:
        raise ValueError("Engine does not belong to the selected generation")
    if modification_id and modification_id not in engine["modification_ids"]:
        raise ValueError("Modification does not belong to the selected engine")
    return KnowledgeIdentity(
        canonical_brand_id=canonical.canonical_brand_id,
        canonical_model_id=canonical.canonical_model_id,
        canonical_generation_id=generation_id,
        canonical_engine_id=str(engine["id"]) if engine else None,
        canonical_modification_id=modification_id,
        brand=normalize_display_text(canonical.brand),
        model=normalize_display_text(canonical.model),
        generation=normalize_display_text(generation["name"]) if generation else "",
        engine=normalize_display_text(engine["label"]) if engine else "",
        generation_year_from=(
            int(generation["year_from"]) if generation and generation.get("year_from") else None
        ),
        generation_year_to=(
            int(generation["year_to"]) if generation and generation.get("year_to") else None
        ),
    )
