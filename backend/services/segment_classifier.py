"""Lazy, canonical vehicle-segment classification independent of listing price."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from backend.database.db import connect
from backend.models.car import BodyType
from backend.services.marketplace_catalog import CatalogCache

SegmentCode = Literal[
    "A", "B", "C", "D", "E", "F", "J-B", "J-C", "J-D", "J-E", "J-F", "M", "S", "UNKNOWN"
]

SEGMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_segments (
 canonical_model_id TEXT NOT NULL,
 canonical_generation_id TEXT NOT NULL DEFAULT '',
 segment_code TEXT NOT NULL,
 segment_family TEXT NOT NULL,
 segment_size TEXT,
 market_position TEXT NOT NULL,
 segment_method TEXT NOT NULL,
 segment_confidence TEXT NOT NULL,
 PRIMARY KEY (canonical_model_id, canonical_generation_id)
);
"""
_CODES = frozenset({"A", "B", "C", "D", "E", "F", "J-B", "J-C", "J-D", "J-E", "J-F", "M", "S"})
_POSITIONS = frozenset({"mainstream", "premium", "luxury"})
_PASSENGER = frozenset(
    {
        BodyType.SEDAN,
        BodyType.WAGON,
        BodyType.HATCHBACK,
        BodyType.LIFTBACK,
        BodyType.COUPE,
        BodyType.CONVERTIBLE,
    }
)
_SUV = frozenset({BodyType.SUV, BodyType.CROSSOVER})


@dataclass(frozen=True, slots=True)
class SegmentClassification:
    segment_code: SegmentCode = "UNKNOWN"
    segment_family: str = "unknown"
    segment_size: str | None = None
    market_position: str = "unknown"
    segment_method: str = "unknown"
    segment_confidence: str = "low"
    cache_hit: bool = False
    ai_fallback_called: bool = False


def _family(body: BodyType | None, metadata: dict) -> str:
    category = str(metadata.get("category") or metadata.get("segment_family") or "").casefold()
    if category in {"passenger", "suv", "mpv", "sport"}:
        return category
    if body in _PASSENGER:
        return "passenger"
    if body in _SUV:
        return "suv"
    if body == BodyType.MINIVAN:
        return "mpv"
    return "unknown"


def _dimension_mm(metadata: dict, *keys: str) -> float | None:
    dimensions = metadata.get("dimensions") or {}
    if not isinstance(dimensions, dict):
        dimensions = {}
    for key in keys:
        value = dimensions.get(key, metadata.get(key))
        if value is None:
            continue
        try:
            number = float(str(value).replace(",", "."))
        except ValueError:
            continue
        if 2 <= number < 10:  # metres
            number *= 1000
        if 1800 <= number <= 7000:
            return number
    return None


def _size_for(value: float, boundaries: tuple[int, ...]) -> str:
    return next(
        (code for code, limit in zip("ABCDE", boundaries, strict=True) if value <= limit), "F"
    )


def classify_metadata(metadata: dict, body: BodyType | None) -> SegmentClassification:
    """Explicit source metadata wins; dimensions require two agreeing signals."""
    family = _family(body, metadata)
    position = str(
        metadata.get("market_position") or metadata.get("positioning") or "unknown"
    ).casefold()
    if position not in _POSITIONS:
        position = "unknown"
    raw = str(metadata.get("segment_code") or metadata.get("segment") or "").upper().strip()
    raw = re.sub(r"\s+", "", raw).replace("J_", "J-")
    if raw in _CODES:
        source_family = (
            "suv"
            if raw.startswith("J-")
            else "mpv"
            if raw == "M"
            else "sport"
            if raw == "S"
            else "passenger"
        )
        if family == "unknown" or family == source_family:
            return SegmentClassification(
                raw,
                source_family,
                raw[-1] if raw[-1] in "ABCDEF" else None,
                position,
                "source",
                "high",
            )
    if family == "mpv":
        return SegmentClassification("M", family, None, position, "rules", "medium")
    if family == "sport" and str(metadata.get("vehicle_type", "")).casefold() == "sport":
        return SegmentClassification("S", family, None, position, "rules", "medium")
    if family not in {"passenger", "suv"}:
        return SegmentClassification(segment_family=family, market_position=position)
    length = _dimension_mm(metadata, "length_mm", "overall_length_mm", "length")
    wheelbase = _dimension_mm(metadata, "wheelbase_mm", "wheelbase")
    if length is None or wheelbase is None:
        return SegmentClassification(segment_family=family, market_position=position)
    if family == "suv":
        length_size = _size_for(length, (4250, 4500, 4750, 5000, 5250))
        wheelbase_size = _size_for(wheelbase, (2500, 2650, 2800, 2950, 3100))
        if length_size == "A":
            length_size = "B"
        if wheelbase_size == "A":
            wheelbase_size = "B"
    else:
        length_size = _size_for(length, (3750, 4200, 4550, 4850, 5100))
        wheelbase_size = _size_for(wheelbase, (2450, 2600, 2750, 2880, 3000))
    if abs("ABCDEF".index(length_size) - "ABCDEF".index(wheelbase_size)) > 1:
        return SegmentClassification(segment_family=family, market_position=position)
    # Length and wheelbase may straddle a class boundary; do not claim high certainty.
    confidence = "medium" if length_size == wheelbase_size else "low"
    code = f"J-{length_size}" if family == "suv" else length_size
    return SegmentClassification(code, family, length_size, position, "dimensions", confidence)


AiFallback = Callable[[dict, BodyType | None], Awaitable[SegmentClassification | None]]


def dimensions_from_catalog_html(html: str) -> dict[str, int]:
    """Extract only labelled dimensions; unrelated specification numbers are ignored."""
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)

    def find(pattern: str) -> int | None:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = float(match.group(1).replace(",", "."))
            if value < 10:
                value *= 1000
            number = round(value)
            if 2200 <= number <= 6000:
                return number
        return None

    length = find(
        r"(?:длина\s+кузова|длина\s+автомобиля|\bдлина\b|overall\s+length|\blength\b)"
        r"[^\d]{0,75}(\d{4}|\d[,\.]\d{1,2})\s*(?:мм|метра|метров|м\b)?"
    )
    wheelbase = find(
        r"(?:кол[её]сн\w*\s+баз\w*|межосев\w*\s+расстояни\w*|wheelbase)"
        r"[^\d]{0,90}(\d{4}|\d[,\.]\d{1,2})\s*(?:мм|метра|метров|м\b)?"
    )
    return {"length_mm": length, "wheelbase_mm": wheelbase} if length and wheelbase else {}


class SegmentClassifier:
    def __init__(
        self,
        catalog: CatalogCache,
        database_path: Path | None = None,
        ai_fallback: AiFallback | None = None,
    ) -> None:
        self.catalog = catalog
        self.database_path = database_path
        self.ai_fallback = ai_fallback
        self._memory: dict[tuple[str, str], SegmentClassification] = {}
        self._source_attempted: set[tuple[str, str]] = set()

    async def _source_dimensions(
        self, model_id: str, generation: dict, generation_key: str
    ) -> dict[str, int]:
        scope = model_id, generation_key
        if scope in self._source_attempted:
            return {}
        self._source_attempted.add(scope)
        refs = sorted(
            generation.get("source_refs", []),
            key=lambda ref: 0 if ref.get("source") == "drom.ru" else 1,
        )
        for ref in refs[:2]:
            url = str(ref.get("url") or "")
            host = urlsplit(url).hostname
            if urlsplit(url).scheme != "https" or host not in {
                "drom.ru",
                "www.drom.ru",
                "auto.ru",
                "www.auto.ru",
            }:
                continue
            try:
                async with httpx.AsyncClient(timeout=3.5, follow_redirects=False) as client:
                    response = await client.get(url)
                if (
                    response.status_code != 200
                    or "text/html" not in response.headers.get("content-type", "")
                    or len(response.content) > 2_000_000
                ):
                    continue
                dimensions = dimensions_from_catalog_html(response.text)
                if dimensions:
                    return dimensions
            except httpx.HTTPError:
                continue
        return {}

    def _read(self, model_id: str, generation_id: str) -> SegmentClassification | None:
        key = model_id, generation_id
        if key in self._memory:
            return replace(self._memory[key], cache_hit=True)
        with connect(self.database_path) as connection:
            connection.executescript(SEGMENT_SCHEMA)
            row = connection.execute(
                "SELECT * FROM vehicle_segments WHERE canonical_model_id=? "
                "AND canonical_generation_id=?",
                key,
            ).fetchone()
            if (
                row
                and row["segment_method"] == "dimensions"
                and row["segment_confidence"] == "high"
            ):
                connection.execute(
                    "UPDATE vehicle_segments SET segment_confidence='medium' "
                    "WHERE canonical_model_id=? AND canonical_generation_id=?",
                    key,
                )
        if row is None:
            return None
        result = SegmentClassification(
            row["segment_code"],
            row["segment_family"],
            row["segment_size"],
            row["market_position"],
            row["segment_method"],
            "medium"
            if row["segment_method"] == "dimensions" and row["segment_confidence"] == "high"
            else row["segment_confidence"],
        )
        self._memory[key] = result
        return replace(result, cache_hit=True)

    def _save(self, model_id: str, generation_id: str, result: SegmentClassification) -> None:
        if result.segment_code == "UNKNOWN":
            return
        key = model_id, generation_id
        with connect(self.database_path) as connection:
            connection.executescript(SEGMENT_SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO vehicle_segments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *key,
                    result.segment_code,
                    result.segment_family,
                    result.segment_size,
                    result.market_position,
                    result.segment_method,
                    result.segment_confidence,
                ),
            )
        self._memory[key] = result

    async def classify(
        self,
        brand: str,
        model: str,
        generation_id: str | None,
        body: BodyType | None,
        *,
        allow_ai: bool = False,
        allow_source_fetch: bool = False,
    ) -> SegmentClassification:
        identity = self.catalog.resolve_identity(brand, model)
        if identity.provisional:
            return classify_metadata({}, body)
        model_id = identity.canonical_model_id
        generation = self.catalog.generation(identity.brand, identity.model, generation_id)
        generation_key = str(generation["id"]) if generation else ""
        cached = self._read(model_id, generation_key)
        if cached:
            return cached
        model_entry = self.catalog.model_entry(identity.brand, identity.model) or {}
        if body is None and generation:
            possible_bodies = {
                BodyType(value)
                for value in generation.get("body_types", [])
                if value in BodyType._value2member_map_
            }
            families = {_family(item, {}) for item in possible_bodies}
            if len(families) == 1 and possible_bodies:
                body = sorted(possible_bodies, key=str)[0]
        model_meta = dict(model_entry.get("classification") or {})
        generation_meta = dict(generation.get("classification") or {}) if generation else {}
        metadata = {**model_meta, **generation_meta}
        if generation:
            metadata.update(
                {
                    key: generation[key]
                    for key in ("dimensions", "length_mm", "wheelbase_mm")
                    if key in generation
                }
            )
        result = classify_metadata(metadata, body)
        if result.segment_code == "UNKNOWN" and generation and allow_source_fetch:
            try:
                dimensions = await asyncio.wait_for(
                    self._source_dimensions(model_id, generation, generation_key), timeout=4.0
                )
            except TimeoutError:
                dimensions = {}
            if dimensions:
                metadata = {**metadata, "dimensions": dimensions}
                result = classify_metadata(metadata, body)
        if result.segment_code == "UNKNOWN" and generation_key:
            model_cached = self._read(model_id, "")
            if model_cached:
                return model_cached
            model_result = classify_metadata(model_meta, body)
            if model_result.segment_code != "UNKNOWN":
                self._save(model_id, "", model_result)
                return model_result
        has_ambiguous_dimensions = bool(
            _dimension_mm(metadata, "length_mm", "overall_length_mm", "length")
            and _dimension_mm(metadata, "wheelbase_mm", "wheelbase")
        )
        if (
            result.segment_code == "UNKNOWN"
            and allow_ai
            and self.ai_fallback
            and has_ambiguous_dimensions
        ):
            try:
                proposed = await asyncio.wait_for(self.ai_fallback(metadata, body), timeout=4)
                if (
                    proposed
                    and proposed.segment_code in _CODES
                    and proposed.segment_family == _family(body, metadata)
                ):
                    result = replace(
                        proposed,
                        segment_method="ai_fallback",
                        segment_confidence="low",
                        ai_fallback_called=True,
                    )
            except Exception:  # Provider errors must never block market search.
                result = replace(result, ai_fallback_called=True)
        if result.segment_code != "UNKNOWN":
            self._save(model_id, generation_key, result)
        return result
