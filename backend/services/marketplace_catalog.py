from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from backend.config import get_settings
from backend.models.car import BodyType
from backend.models.catalog import (
    CanonicalVehicleIdentity,
    CatalogBrand,
    CatalogModel,
    EngineSpec,
    SourceReference,
    VehicleGeneration,
    VehicleModification,
)
from backend.services.normalizer import Normalizer
from backend.services.reference_data import DATA_ROOT

CATALOG_SEED_PATH = DATA_ROOT / "vehicle_catalog_seed.json"
CATALOG_RUNTIME_PATH = DATA_ROOT.parent.parent / "data" / "vehicle_catalog_runtime.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
DETAIL_TTL = timedelta(days=90)
CATALOG_PARSER_VERSION = 3
_CATALOG_WRITE_LOCK = asyncio.Lock()
_MODEL_ENRICH_LOCKS: dict[str, asyncio.Lock] = {}


@contextmanager
def _catalog_file_lock(target: Path):
    """Serialize read/merge/write across app and CLI processes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(".lock")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(slots=True)
class SourceModel:
    name: str
    url: str


@dataclass(slots=True)
class SourceBrand:
    name: str
    url: str
    models: list[SourceModel]


@dataclass(slots=True)
class SourceCatalog:
    source: str
    brands: list[SourceBrand]
    status: str = "ok"
    detail: str = ""


class CatalogSource(Protocol):
    name: str

    async def fetch(self) -> SourceCatalog: ...


@dataclass(slots=True)
class PublicHtmlCatalogSource:
    name: str
    index_url: str
    brand_prefix: tuple[str, ...]
    concurrency: int = 8
    timeout: float = 25.0
    excluded_brand_names: frozenset[str] = frozenset()
    excluded_model_slugs: frozenset[str] = frozenset()

    async def fetch(self) -> SourceCatalog:
        limits = httpx.Limits(max_connections=self.concurrency)
        async with httpx.AsyncClient(
            # The public index currently renders brand anchors for this minimal
            # identifier; a fabricated Chrome signature can return a JS shell.
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=self.timeout,
            limits=limits,
        ) as client:
            index = await self._get(client, self.index_url)
            brand_links = self._brand_links(index)
            if not brand_links:
                raise ValueError(f"{self.name} returned no catalog brand links")
            semaphore = asyncio.Semaphore(self.concurrency)

            async def fetch_brand(brand: str, url: str) -> tuple[SourceBrand | None, str | None]:
                async with semaphore:
                    try:
                        html = await self._get(client, url)
                        models = self._models(html, url, brand)
                        return SourceBrand(brand, url, models) if models else None, None
                    except (httpx.HTTPError, ValueError) as error:
                        return None, str(error)

            results = await asyncio.gather(
                *(fetch_brand(brand, url) for brand, url in brand_links.items())
            )

        brands = [brand for brand, _ in results if brand is not None]
        if not brands:
            raise ValueError(f"{self.name} returned no catalog models")
        failures = [error for _, error in results if error]
        status = "ok" if not failures else "partial"
        detail = f"{len(brands)} brands"
        if failures:
            detail += f"; {len(failures)} brand pages unavailable"
        return SourceCatalog(self.name, brands, status, detail)

    async def _get(self, client: httpx.AsyncClient, url: str) -> bytes:
        response = await client.get(url)
        response.raise_for_status()
        return response.content

    def _brand_links(self, html: str | bytes) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, url, parts in self._catalog_links(html):
            if (
                name.casefold() not in self.excluded_brand_names
                and len(parts) == len(self.brand_prefix) + 1
                and tuple(parts[:-1]) == self.brand_prefix
            ):
                result.setdefault(name, url)
        return result

    def _models(self, html: str | bytes, brand_url: str, brand: str) -> list[SourceModel]:
        brand_parts = self._path_parts(brand_url)
        by_url: dict[str, SourceModel] = {}
        for name, url, parts in self._catalog_links(html):
            if len(parts) != len(brand_parts) + 1 or parts[:-1] != brand_parts:
                continue
            if parts[-1].casefold() in self.excluded_model_slugs:
                continue
            canonical = _strip_brand_prefix(name, brand)
            if canonical:
                by_url.setdefault(url, SourceModel(canonical, url))
        return list(by_url.values())

    def _catalog_links(self, html: str | bytes) -> list[tuple[str, str, list[str]]]:
        soup = BeautifulSoup(html, "lxml")
        links: list[tuple[str, str, list[str]]] = []
        for anchor in soup.select("a[href]"):
            name = " ".join(anchor.get_text(" ", strip=True).split())
            url = urljoin(self.index_url, anchor.get("href", "")).split("?", 1)[0]
            if name and url.startswith("http"):
                links.append((name, url, self._path_parts(url)))
        return links

    @staticmethod
    def _path_parts(url: str) -> list[str]:
        return [part for part in urlparse(url).path.split("/") if part]


def auto_ru_source() -> PublicHtmlCatalogSource:
    return PublicHtmlCatalogSource(
        name="auto.ru",
        index_url="https://auto.ru/catalog/cars/",
        brand_prefix=("catalog", "cars"),
        excluded_brand_names=frozenset({"китайские", "спортивные авто и реплики"}),
        excluded_model_slugs=frozenset({"engine", "photo"}),
    )


def drom_source() -> PublicHtmlCatalogSource:
    return PublicHtmlCatalogSource(
        name="drom.ru",
        index_url="https://www.drom.ru/catalog/",
        brand_prefix=("catalog",),
        excluded_brand_names=frozenset({"грузовики и спецтехника", "каталог спецтехники"}),
        excluded_model_slugs=frozenset({"engine", "frame"}),
    )


BRAND_ALIASES = {
    "lada": "Lada",
    "ladaваз": "Lada",
    "вазlada": "Lada",
    "лада": "Lada",
    "mercedes": "Mercedes-Benz",
    "mercedesbenz": "Mercedes-Benz",
    "мерседесбенц": "Mercedes-Benz",
    "volkswagen": "Volkswagen",
    "vw": "Volkswagen",
    "фольксваген": "Volkswagen",
}


def _key(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", value.casefold())


def _brand_name(value: str) -> str:
    return BRAND_ALIASES.get(_key(value), value.strip())


def _strip_brand_prefix(name: str, brand: str) -> str:
    return re.sub(rf"^{re.escape(brand)}\s+", "", name.strip(), flags=re.I).strip()


def _model_key(value: str) -> str:
    normalized = _semantic_model_text(value)
    key = _key(normalized)
    match = re.fullmatch(r"(\d+)er", key)
    return f"{match.group(1)}series" if match else key


def _semantic_model_text(value: str) -> str:
    """Keep punctuation that distinguishes model identities, without name lists."""
    folded = value.casefold().replace("серии", "series").replace("серия", "series")
    folded = folded.replace("+", "plus")
    folded = re.sub(r"(?<=\d)-(?=\d)", "dash", folded)
    return re.sub(r"(?<=[0-9a-zа-яё])/(?=[0-9a-zа-яё])", "slash", folded)


_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "x",
        "ц": "c",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


def _identity_keys(value: str) -> frozenset[str]:
    """Exact language/script-normalized keys; never prefix/substring guesses."""

    folded = _semantic_model_text(value)
    compact = _key(folded)
    # Russian loan spelling "рей" and English "ray" are phonetic equivalents.
    phonetic = folded.replace("ей", "ай").translate(_CYRILLIC_TO_LATIN)
    transliterated = re.sub(r"[^0-9a-z]+", "", phonetic)
    return frozenset(key for key in (compact, transliterated) if key)


def _source_ref(source: str, url: str) -> SourceReference:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    return SourceReference(
        source=source,
        url=url,
        path=parsed.path,
        slug=parts[-1] if parts else "",
    )


@dataclass(slots=True)
class CatalogCache:
    # `path` remains the first argument for isolated tests. Production reads an
    # immutable tracked seed and writes enrichment only to the ignored runtime cache.
    path: Path | None = None
    seed_path: Path = CATALOG_SEED_PATH
    runtime_path: Path = field(
        default_factory=lambda: get_settings().resolved_catalog_runtime_path()
    )
    _payload: dict[str, object] | None = field(default=None, init=False)
    _mtime_ns: int | None = field(default=None, init=False)
    _loaded_path: Path | None = field(default=None, init=False)

    def read_path(self) -> Path:
        if self.path is not None:
            return self.path
        return self.runtime_path if self.runtime_path.exists() else self.seed_path

    def write_path(self) -> Path:
        return self.path if self.path is not None else self.runtime_path

    def load(self) -> dict[str, object]:
        active_path = self.read_path()
        if not active_path.exists():
            self._payload = {"version": 3, "sources": {}, "brands": []}
            self._mtime_ns = None
            self._loaded_path = active_path
            return self._payload
        mtime_ns = active_path.stat().st_mtime_ns
        if self._payload is None or self._mtime_ns != mtime_ns or self._loaded_path != active_path:
            self._payload = json.loads(active_path.read_text(encoding="utf-8"))
            self._mtime_ns = mtime_ns
            self._loaded_path = active_path
        return self._payload

    def brands(self) -> list[str]:
        return [entry["name"] for entry in self.load().get("brands", [])]  # type: ignore[index]

    def model_entries(self, brand: str) -> list[dict[str, object]]:
        entry = self._brand_entry(brand)
        return list(entry.get("models", [])) if entry else []  # type: ignore[arg-type]

    def models(self, brand: str) -> list[str]:
        return [str(model["name"]) for model in self.model_entries(brand)]

    def brand_entry(self, brand: str) -> dict[str, object] | None:
        return self._brand_entry(brand)

    def model_entry(self, brand: str, model: str) -> dict[str, object] | None:
        entries = self.model_entries(brand)
        needle_keys = _identity_keys(model)
        canonical_id_matches = [entry for entry in entries if str(entry.get("id")) == model]
        if len(canonical_id_matches) == 1:
            return canonical_id_matches[0]

        # The selector submits the stored display name. It must win over an
        # unrelated model's historical source slug with the same loose spelling.
        exact_name_matches = [
            entry for entry in entries if str(entry.get("name", "")).casefold() == model.casefold()
        ]
        if len(exact_name_matches) == 1:
            return exact_name_matches[0]

        # Stable source slugs/paths take priority over display strings.
        source_matches = []
        for entry in entries:
            source_keys = {
                key
                for ref in entry.get("source_refs", [])
                for key in _identity_keys(str(ref.get("slug", "")))
            }
            if needle_keys & source_keys:
                source_matches.append(entry)
        if len(source_matches) == 1:
            return source_matches[0]
        if len(source_matches) > 1:
            return None

        name_matches = []
        for entry in entries:
            values = [str(entry["name"]), *(str(value) for value in entry.get("aliases", []))]
            keys = {key for value in values for key in _identity_keys(value)}
            if needle_keys & keys:
                name_matches.append(entry)
        if len(name_matches) == 1:
            return name_matches[0]
        if len(name_matches) > 1:
            return None

        # A numeric trim (for example 520i) may identify an ordinal series only
        # when the catalog has exactly one exact ordinal-family entry. This is
        # deliberately not prefix matching: derived families remain distinct.
        compact = _model_key(model)
        numeric = re.fullmatch(r"([1-9])\d{2}[a-z]*", compact)
        if numeric:
            ordinal_keys = {f"{numeric.group(1)}series", f"{numeric.group(1)}er"}
            family_matches = []
            for entry in entries:
                values = [
                    str(entry["name"]),
                    *(str(value) for value in entry.get("aliases", [])),
                    *(str(ref.get("slug", "")) for ref in entry.get("source_refs", [])),
                ]
                if ordinal_keys & {key for value in values for key in _identity_keys(value)}:
                    family_matches.append(entry)
            if len(family_matches) == 1:
                return family_matches[0]
        return None

    def resolve_identity(self, brand: str, model: str) -> CanonicalVehicleIdentity:
        brand_entry = self._brand_entry(brand)
        provisional_brand = _key(_brand_name(brand)) or "unknown"
        if brand_entry is None:
            provisional_model = _model_key(model) or "unknown"
            return CanonicalVehicleIdentity(
                canonical_brand_id=f"provisional:{provisional_brand}",
                canonical_model_id=f"provisional:{provisional_brand}:{provisional_model}",
                brand=Normalizer.brand(brand),
                model=Normalizer.model(model),
                provisional=True,
            )

        needle = _model_key(model)
        match = self.model_entry(str(brand_entry["name"]), model)

        if match is None:
            provisional_model = needle or "unknown"
            return CanonicalVehicleIdentity(
                canonical_brand_id=str(brand_entry["id"]),
                canonical_model_id=f"provisional:{brand_entry['id']}:{provisional_model}",
                brand=str(brand_entry["name"]),
                model=Normalizer.model(model),
                provisional=True,
            )
        return CanonicalVehicleIdentity(
            canonical_brand_id=str(brand_entry["id"]),
            canonical_model_id=str(match["id"]),
            brand=str(brand_entry["name"]),
            model=str(match["name"]),
        )

    def source_model_ref(self, source: str, brand: str, model: str) -> SourceReference | None:
        entry = self.model_entry(brand, model)
        if not entry:
            return None
        for raw in entry.get("source_refs", []):
            ref = SourceReference.model_validate(raw)
            if ref.source == source:
                return ref
        return None

    def generations(
        self,
        brand: str,
        model: str,
        year: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> list[dict[str, object]]:
        entry = self.model_entry(brand, model)
        generations = list(entry.get("generations", [])) if entry else []
        if year is not None:
            year_from = year_to = year
        if year_from is None or year_to is None:
            return generations
        return [
            item
            for item in generations
            if (item.get("year_from") is None or int(item["year_from"]) <= year_to)
            and (item.get("year_to") is None or int(item["year_to"]) >= year_from)
        ]

    def modifications(
        self,
        brand: str,
        model: str,
        year: int | None = None,
        generation_id: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> list[dict[str, object]]:
        generations = self.generations(brand, model, year, year_from, year_to)
        if generation_id:
            generations = [item for item in generations if item["id"] == generation_id]
        result: list[dict[str, object]] = []
        seen: set[str] = set()
        for generation in generations:
            for modification in generation.get("modifications", []):
                identifier = str(modification["id"])
                if identifier not in seen:
                    seen.add(identifier)
                    result.append(modification)
        return result

    @staticmethod
    def engine_key(modification: dict[str, object]) -> str:
        engine = modification.get("engine") or {}
        normalized = "|".join(
            str(engine.get(field) or "").strip().casefold()
            for field in ("fuel_type", "displacement_l", "power_hp", "engine_code")
        )
        return f"engine:{hashlib.sha256(normalized.encode()).hexdigest()[:16]}"

    def engine_options(
        self,
        brand: str,
        model: str,
        year: int | None = None,
        generation_id: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> list[dict[str, object]]:
        grouped: dict[str, dict[str, object]] = {}
        for modification in self.modifications(
            brand, model, year, generation_id, year_from, year_to
        ):
            key = self.engine_key(modification)
            option = grouped.setdefault(
                key,
                {
                    "id": key,
                    "engine": modification.get("engine") or {},
                    "modification_ids": [],
                    "source_refs": [],
                },
            )
            option["modification_ids"].append(str(modification["id"]))
            known_refs = {
                (str(item.get("source")), str(item.get("path"))) for item in option["source_refs"]
            }
            option["source_refs"].extend(
                ref
                for ref in modification.get("source_refs", [])
                if (str(ref.get("source")), str(ref.get("path"))) not in known_refs
            )
        for option in grouped.values():
            engine = EngineSpec.model_validate(option["engine"])
            parts = []
            if engine.displacement_l is not None:
                parts.append(f"{engine.displacement_l:g} л")
            if engine.fuel_type:
                parts.append(engine.fuel_type)
            if engine.power_hp is not None:
                parts.append(f"{engine.power_hp} л.с.")
            if engine.engine_code:
                parts.append(engine.engine_code)
            option["label"] = " · ".join(parts) or "Двигатель не указан"
        return sorted(grouped.values(), key=lambda item: str(item["label"]).casefold())

    def engine_option(
        self,
        brand: str,
        model: str,
        engine_id: str | None,
        generation_id: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> dict[str, object] | None:
        if not engine_id:
            return None
        return next(
            (
                option
                for option in self.engine_options(
                    brand,
                    model,
                    generation_id=generation_id,
                    year_from=year_from,
                    year_to=year_to,
                )
                if option["id"] == engine_id or engine_id in option["modification_ids"]
            ),
            None,
        )

    def generation(
        self, brand: str, model: str, generation_id: str | None
    ) -> dict[str, object] | None:
        if not generation_id:
            return None
        return next(
            (
                item
                for item in self.generations(brand, model)
                if str(item.get("id")) == generation_id
            ),
            None,
        )

    def modification(self, modification_id: str | None) -> dict[str, object] | None:
        if not modification_id:
            return None
        for brand in self.load().get("brands", []):  # type: ignore[union-attr]
            for model in brand.get("models", []):
                for generation in model.get("generations", []):
                    for modification in generation.get("modifications", []):
                        if modification["id"] == modification_id:
                            return modification
        return None

    def status(self) -> dict[str, object]:
        payload = self.load()
        brands = payload.get("brands", [])
        carsbase = payload.get("sources", {}).get("cars-base.ru", {})
        generations = [
            generation
            for brand in brands
            for model in brand.get("models", [])
            for generation in model.get("generations", [])
        ]
        return {
            "catalog_updated_at": payload.get("catalog_updated_at"),
            "brand_count": len(brands),
            "model_count": sum(len(entry.get("models", [])) for entry in brands),
            "generation_count": len(generations),
            "modification_count": sum(len(item.get("modifications", [])) for item in generations),
            "sources": payload.get("sources", {}),
            "carsbase": carsbase,
        }

    def save(self, payload: dict[str, object]) -> None:
        target = self.write_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            # Windows readers can briefly prevent replacement even though the
            # writer lock serializes all catalog writers. Keep the fully written
            # temp file and retry only this local atomic step, never the fetch.
            for attempt in range(6):
                try:
                    os.replace(temporary_path, target)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * 2**attempt)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        self._payload = payload
        self._mtime_ns = target.stat().st_mtime_ns
        self._loaded_path = target

    def save_model_details(
        self, brand: str, model: str, generations: list[VehicleGeneration]
    ) -> None:
        self._save_model_observation(brand, model, "complete", generations)

    @staticmethod
    def _merge_generation(current: dict, incoming: dict) -> dict:
        def refs(values: list[dict], additions: list[dict]) -> list[dict]:
            unique = {(str(value.get("source")), str(value.get("path"))): value for value in values}
            unique.update(
                {(str(value.get("source")), str(value.get("path"))): value for value in additions}
            )
            return list(unique.values())

        firsts = [x for x in (current.get("year_from"), incoming.get("year_from")) if x is not None]
        # A known open end remains open; an unknown end also remains unknown.
        ends = (current.get("year_to"), incoming.get("year_to"))
        current["year_from"] = min(firsts) if firsts else None
        current["year_to"] = None if None in ends else max(ends)
        current["body_types"] = sorted(
            set(current.get("body_types", [])) | set(incoming.get("body_types", []))
        )
        current["source_refs"] = refs(
            current.get("source_refs", []), incoming.get("source_refs", [])
        )
        by_id = {item["id"]: item for item in current.get("modifications", [])}
        for item in incoming.get("modifications", []):
            existing = by_id.get(item["id"])
            if existing is None:
                by_id[item["id"]] = item
            else:
                existing["source_refs"] = refs(
                    existing.get("source_refs", []), item.get("source_refs", [])
                )
        current["modifications"] = list(by_id.values())
        return current

    def _save_model_observation(
        self,
        brand: str,
        model: str,
        status: str,
        generations: list[VehicleGeneration] | None = None,
        error_type: str | None = None,
        error_detail: str | None = None,
        replace_source: str | None = None,
    ) -> None:
        payload = self.load()
        entry = self.model_entry(brand, model)
        if entry is None:
            return
        existing = {
            generation["id"]: generation
            for generation in entry.get("generations", [])
            if not (
                replace_source
                and generation.get("source_refs")
                and all(ref.get("source") == replace_source for ref in generation["source_refs"])
            )
        }
        for generation in generations or []:
            incoming = generation.model_dump(mode="json")
            if generation.id in existing:
                self._merge_generation(existing[generation.id], incoming)
            else:
                existing[generation.id] = incoming
        entry["generations"] = list(existing.values())
        now = datetime.now(UTC)
        entry["details_status"] = status
        entry["details_checked_at"] = now.isoformat()
        entry["details_parser_version"] = CATALOG_PARSER_VERSION
        entry["details_error_type"] = error_type
        entry["details_error_detail"] = error_detail
        ttl = {
            "complete": DETAIL_TTL,
            "source_has_no_generation_data": timedelta(days=30),
            "parse_error": timedelta(hours=12),
            "source_unavailable": timedelta(days=1),
            "rate_limited": timedelta(hours=2),
        }.get(status, timedelta(hours=1))
        if status == "rate_limited" and error_detail:
            try:
                ttl = timedelta(seconds=max(1, int(error_detail)))
            except ValueError:
                with suppress(TypeError, ValueError):
                    ttl = max(timedelta(seconds=1), parsedate_to_datetime(error_detail) - now)
        entry["details_retry_at"] = (now + ttl).isoformat()
        if status == "complete":
            entry["details_updated_at"] = now.isoformat()
        self.save(payload)

    async def merge_model_observation(
        self,
        brand: str,
        model: str,
        status: str,
        generations: list[VehicleGeneration] | None = None,
        error_type: str | None = None,
        error_detail: str | None = None,
        replace_source: str | None = None,
    ) -> None:
        async with _CATALOG_WRITE_LOCK:
            with _catalog_file_lock(self.write_path()):
                self._payload = None
                self._save_model_observation(
                    brand, model, status, generations, error_type, error_detail, replace_source
                )

    async def merge_source_mapping(
        self,
        source: str,
        brand: str,
        model: str,
        brand_ref: SourceReference,
        model_ref: SourceReference,
    ) -> None:
        async with _CATALOG_WRITE_LOCK:
            with _catalog_file_lock(self.write_path()):
                self._payload = None
                payload = self.load()
                brand_entry = self._brand_entry(brand)
                model_entry = self.model_entry(brand, model)
                if brand_entry is None or model_entry is None:
                    return

                def add_ref(entry: dict[str, object], ref: SourceReference) -> None:
                    refs = list(entry.get("source_refs", []))
                    serialized = ref.model_dump(mode="json")
                    refs = [
                        value
                        for value in refs
                        if not (
                            value.get("source") == source
                            and value.get("path") == serialized["path"]
                        )
                    ]
                    refs.append(serialized)
                    entry["source_refs"] = refs

                add_ref(brand_entry, brand_ref)
                add_ref(model_entry, model_ref)
                self.save(payload)

    def details_are_fresh(self, brand: str, model: str, year: int) -> bool:
        entry = self.model_entry(brand, model)
        if not entry or not self.generations(brand, model, year):
            return False
        raw = entry.get("details_updated_at")
        return bool(raw and datetime.now(UTC) - datetime.fromisoformat(str(raw)) <= DETAIL_TTL)

    def details_are_fresh_window(self, brand: str, model: str, start: int, end: int) -> bool:
        entry = self.model_entry(brand, model)
        if not entry:
            return False
        # Legacy snapshots have a timestamp but no observation state. They
        # must be checked once before being eligible for a validated audit.
        if entry.get("details_status") in (None, "not_checked"):
            return False
        if (
            entry.get("details_status") in ("complete", "source_has_no_generation_data")
            and entry.get("details_parser_version") != CATALOG_PARSER_VERSION
        ):
            return False
        retry_at = entry.get("details_retry_at")
        if retry_at:
            return datetime.now(UTC) < datetime.fromisoformat(str(retry_at))
        raw = entry.get("details_updated_at")
        return bool(raw and datetime.now(UTC) - datetime.fromisoformat(str(raw)) <= DETAIL_TTL)

    def _brand_entry(self, brand: str) -> dict[str, object] | None:
        needle = _key(_brand_name(brand))
        for entry in self.load().get("brands", []):  # type: ignore[union-attr]
            aliases = entry.get("aliases", [])
            keys = {_key(_brand_name(str(entry["name"]))), *(_key(str(x)) for x in aliases)}
            if needle in keys:
                return entry
        return None


@lru_cache(maxsize=1)
def get_catalog_cache() -> CatalogCache:
    """Share reads while retaining CatalogCache's mtime-based reload behavior."""

    return CatalogCache()


class CatalogSyncService:
    def __init__(
        self,
        sources: list[CatalogSource] | None = None,
        cache: CatalogCache | None = None,
    ) -> None:
        self.sources = sources or [auto_ru_source(), drom_source()]
        self.cache = cache or CatalogCache()

    async def sync(self) -> dict[str, object]:
        results = await asyncio.gather(
            *(source.fetch() for source in self.sources), return_exceptions=True
        )
        available: list[SourceCatalog] = []
        source_status: dict[str, dict[str, str]] = {}
        for source, result in zip(self.sources, results, strict=True):
            if isinstance(result, BaseException):
                source_status[source.name] = {"status": "error", "detail": str(result)}
            else:
                available.append(result)
                source_status[result.source] = {"status": result.status, "detail": result.detail}
        if not available:
            raise RuntimeError(
                "No marketplace catalog source was available; local cache was preserved"
            )

        # A stored source URL is a stronger identity than a display-name key.
        # This also keeps legacy canonical IDs stable after collision repair.
        existing_refs = {
            (str(ref.get("source")), str(ref.get("url"))): str(model["id"])
            for brand in self.cache.load().get("brands", [])
            for model in brand.get("models", [])
            for ref in model.get("source_refs", [])
        }
        merged: dict[str, CatalogBrand] = {}
        model_maps: dict[str, dict[str, CatalogModel]] = {}
        for result in available:
            for source_brand in result.brands:
                brand_name = _brand_name(source_brand.name)
                brand_key = _key(brand_name)
                brand = merged.setdefault(
                    brand_key,
                    CatalogBrand(id=brand_key, name=brand_name, source_refs=[]),
                )
                if source_brand.name != brand.name and source_brand.name not in brand.aliases:
                    brand.aliases.append(source_brand.name)
                brand.source_refs.append(_source_ref(result.source, source_brand.url))
                models = model_maps.setdefault(brand_key, {})
                for source_model in source_brand.models:
                    model_name = _strip_brand_prefix(source_model.name, source_brand.name)
                    key = _model_key(model_name)
                    model_id = existing_refs.get(
                        (result.source, source_model.url), f"{brand_key}:{key}"
                    )
                    if not model_id.startswith(f"{brand_key}:"):
                        model_id = f"{brand_key}:{key}"
                    model = models.setdefault(
                        model_id,
                        CatalogModel(id=model_id, name=model_name, source_refs=[]),
                    )
                    if model_name != model.name and model_name not in model.aliases:
                        model.aliases.append(model_name)
                    model.source_refs.append(_source_ref(result.source, source_model.url))

        for brand_key, brand in merged.items():
            brand.models = sorted(model_maps[brand_key].values(), key=lambda x: x.name.casefold())
        brands = sorted(merged.values(), key=lambda x: x.name.casefold())
        update: dict[str, object] = {
            "version": 3,
            "catalog_updated_at": datetime.now(UTC).isoformat(),
            "sources": source_status,
            "brands": [brand.model_dump(mode="json") for brand in brands],
        }
        async with _CATALOG_WRITE_LOCK:
            with _catalog_file_lock(self.cache.write_path()):
                self.cache._payload = None
                payload = self._merge_payload(deepcopy(self.cache.load()), update)
                self.cache.save(payload)
        return self.cache.status()

    @staticmethod
    def _merge_payload(existing: dict[str, object], update: dict[str, object]) -> dict[str, object]:
        """Merge successful source observations without deleting cached enrichment."""

        def merge_refs(current: list[dict], incoming: list[dict]) -> list[dict]:
            merged = {(str(item.get("source")), str(item.get("path"))): item for item in current}
            for item in incoming:
                merged[(str(item.get("source")), str(item.get("path")))] = item
            return list(merged.values())

        brands = {str(item["id"]): item for item in existing.get("brands", [])}
        for incoming_brand in update.get("brands", []):
            brand_id = str(incoming_brand["id"])
            brand = brands.setdefault(brand_id, incoming_brand)
            if brand is incoming_brand:
                continue
            brand["aliases"] = sorted(
                {*brand.get("aliases", []), *incoming_brand.get("aliases", [])},
                key=str.casefold,
            )
            brand["source_refs"] = merge_refs(
                brand.get("source_refs", []), incoming_brand.get("source_refs", [])
            )
            models = {str(item["id"]): item for item in brand.get("models", [])}
            for incoming_model in incoming_brand.get("models", []):
                model_id = str(incoming_model["id"])
                model = models.setdefault(model_id, incoming_model)
                if model is incoming_model:
                    continue
                model["aliases"] = sorted(
                    {*model.get("aliases", []), *incoming_model.get("aliases", [])},
                    key=str.casefold,
                )
                model["source_refs"] = merge_refs(
                    model.get("source_refs", []), incoming_model.get("source_refs", [])
                )
            brand["models"] = sorted(models.values(), key=lambda item: str(item["name"]).casefold())
        existing_sources = dict(existing.get("sources", {}))
        existing_sources.update(update.get("sources", {}))
        return {
            **existing,
            "version": max(int(existing.get("version", 0)), int(update.get("version", 0))),
            "catalog_updated_at": update.get("catalog_updated_at"),
            "sources": existing_sources,
            "brands": sorted(brands.values(), key=lambda item: str(item["name"]).casefold()),
        }


class CatalogEnrichmentService:
    def __init__(self, cache: CatalogCache | None = None) -> None:
        self.cache = cache or CatalogCache()

    async def ensure(self, brand: str, model: str, year: int) -> None:
        await self.ensure_window(brand, model, year, year)

    async def ensure_window(self, brand: str, model: str, start: int, end: int) -> str:
        del start, end  # A model page is fresh independently of the selected year.
        entry = self.cache.model_entry(brand, model)
        if entry is None:
            return "source_unavailable"
        if self.cache.details_are_fresh_window(brand, model, 0, 0):
            return str(
                entry.get(
                    "details_status", "complete" if entry.get("generations") else "not_checked"
                )
            )
        if self.cache.source_model_ref("drom.ru", brand, model) is None:
            if self.cache.source_model_ref("cars-base.ru", brand, model) is None:
                return "source_not_supported_for_details"
            from backend.services.drom_model_mapping import DromModelResolver

            mapping = await DromModelResolver(self.cache).resolve(brand, model)
            if mapping != "mapped":
                return "rate_limited" if mapping == "rate_limited" else "source_unavailable"
        lock = _MODEL_ENRICH_LOCKS.setdefault(f"{brand}|{model}", asyncio.Lock())
        async with lock:
            self.cache._payload = None
            if self.cache.details_are_fresh_window(brand, model, 0, 0):
                refreshed = self.cache.model_entry(brand, model)
                return (
                    str(refreshed.get("details_status", "complete"))
                    if refreshed
                    else "source_unavailable"
                )
            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30
            ) as client:
                return await self.enrich_model(brand, model, client)

    async def enrich_model(
        self,
        brand: str,
        model: str,
        client: httpx.AsyncClient,
        *,
        retry_failed: bool = False,
    ) -> str:
        entry = self.cache.model_entry(brand, model)
        if entry is None:
            return "source_unavailable"
        if not retry_failed and self.cache.details_are_fresh_window(brand, model, 0, 0):
            return str(
                entry.get(
                    "details_status", "complete" if entry.get("generations") else "not_checked"
                )
            )
        ref = self.cache.source_model_ref("drom.ru", brand, model)
        if ref is None:
            if self.cache.source_model_ref("cars-base.ru", brand, model) is None:
                return "source_not_supported_for_details"
            from backend.services.drom_model_mapping import DromModelResolver

            mapping = await DromModelResolver(self.cache).resolve(brand, model, client)
            if mapping != "mapped":
                return "rate_limited" if mapping == "rate_limited" else "source_unavailable"
            ref = self.cache.source_model_ref("drom.ru", brand, model)
            if ref is None:
                return "source_unavailable"
        url = ref.url.rstrip("/") + "/"
        try:
            response = await client.get(url)
            if response.status_code == 429:
                await self.cache.merge_model_observation(
                    brand,
                    model,
                    "rate_limited",
                    error_type="HTTP_429",
                    error_detail=response.headers.get("Retry-After"),
                )
                return "rate_limited"
            if response.status_code in (403, 503):
                await self.cache.merge_model_observation(
                    brand, model, "source_unavailable", error_type=f"HTTP_{response.status_code}"
                )
                return "source_unavailable"
            response.raise_for_status()
            generations = self.parse_drom_year(response.content, brand, model, 0, url)
            marker = self._has_generation_marker(response.content)
            if not generations:
                state = "parse_error" if marker else "source_has_no_generation_data"
                await self.cache.merge_model_observation(brand, model, state)
                return state
            # Current Drom markup keeps modifications on each generation page.
            # Fetch each unique generation URL once, with the same shared client.
            generation_urls: dict[str, list[VehicleGeneration]] = {}
            for generation in generations:
                for generation_ref in generation.source_refs:
                    generation_urls.setdefault(generation_ref.url, []).append(generation)
            for generation_url, targets in generation_urls.items():
                if any(item.modifications for item in targets):
                    continue
                detail = await client.get(generation_url)
                if detail.status_code == 429:
                    await self.cache.merge_model_observation(
                        brand,
                        model,
                        "rate_limited",
                        generations,
                        error_type="HTTP_429",
                        error_detail=detail.headers.get("Retry-After"),
                    )
                    return "rate_limited"
                detail.raise_for_status()
                for generation in targets:
                    mods = self.parse_drom_generation(
                        detail.content, generation_url, generation.body_types
                    )
                    known = {item.id for item in generation.modifications}
                    generation.modifications.extend(item for item in mods if item.id not in known)
            await self.cache.merge_model_observation(
                brand, model, "complete", generations, replace_source="drom.ru"
            )
            return "complete"
        except (httpx.HTTPError, ValueError) as exc:
            state = "source_unavailable" if isinstance(exc, httpx.HTTPError) else "parse_error"
            error_type = (
                f"HTTP_{exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else type(exc).__name__
            )
            await self.cache.merge_model_observation(
                brand, model, state, error_type=error_type, error_detail=str(exc)[:240]
            )
            return state

    @staticmethod
    def _has_generation_marker(content: bytes) -> bool:
        soup = BeautifulSoup(content, "lxml")
        return bool(
            soup.select('[data-ga-stats-name="generations_outlet_item"]')
            or any(
                re.search(r"поколени[ея]|generations?", heading.get_text(" ", strip=True), re.I)
                for heading in soup.find_all(["h2", "h3"])
            )
        )

    @classmethod
    def parse_drom_year(
        cls, content: bytes, brand: str, model: str, year: int, page_url: str
    ) -> list[VehicleGeneration]:
        del brand, model, year  # Catalog years come only from the source page.
        soup = BeautifulSoup(content, "lxml")
        generations: dict[tuple[str, str | int | None], VehicleGeneration] = {}
        for heading in soup.find_all("h3"):
            # Drom puts all generation sections under one parent. Restrict cards
            # to siblings after this heading and before the next h3; selecting
            # from heading.parent incorrectly assigns every model card to every
            # generation (and can merge the 1999 production start into 2026).
            section_nodes: list[Tag] = []
            for sibling in heading.next_siblings:
                if isinstance(sibling, Tag) and sibling.name == "h3":
                    break
                if isinstance(sibling, Tag):
                    section_nodes.append(sibling)
            cards = [
                card
                for node in section_nodes
                for card in (
                    [node]
                    if node.get("data-ga-stats-name") == "generations_outlet_item"
                    else node.select('[data-ga-stats-name="generations_outlet_item"]')
                )
            ]
            if not cards:
                continue
            section_text = heading.get_text(" ", strip=True)
            ordinal = next(
                (
                    span.get_text(" ", strip=True)
                    for span in section_nodes
                    if span.name == "span"
                    if "поколен" in span.get_text(" ", strip=True).casefold()
                ),
                section_text,
            )
            heading_years = [int(value) for value in re.findall(r"(?:19|20)\d{2}", section_text)]
            code_match = re.search(r",\s*([^,]+),\s*(?:19|20)\d{2}\b", section_text)
            code = code_match.group(1).strip() if code_match else None
            for card in cards:
                link = card.find("a", href=True)
                if not isinstance(link, Tag):
                    continue
                generation_url = urljoin(page_url, str(link.get("href")))
                page_parts = PublicHtmlCatalogSource._path_parts(page_url)
                generation_parts = PublicHtmlCatalogSource._path_parts(generation_url)
                if (
                    urlparse(generation_url).netloc != urlparse(page_url).netloc
                    or len(generation_parts) < 3
                    or not generation_parts[-1].startswith("g_")
                    or generation_parts[-3:-1] != page_parts[-2:]
                ):
                    continue
                caption = card.find(attrs={"data-ftid": "component_article_caption"})
                period_text = caption.get_text(" ", strip=True) if isinstance(caption, Tag) else ""
                period_years = [int(value) for value in re.findall(r"(?:19|20)\d{2}", period_text)]
                year_from = (
                    period_years[0]
                    if period_years
                    else (heading_years[0] if heading_years else None)
                )
                year_to = (
                    period_years[1]
                    if len(period_years) > 1
                    else (heading_years[1] if len(heading_years) > 1 else None)
                )
                body = cls._body_from_text(card.get_text(" ", strip=True))
                key = (ordinal.casefold(), section_text.casefold())
                ref = _source_ref("drom.ru", generation_url)
                candidate = VehicleGeneration(
                    id=f"drom:{ref.slug}",
                    name=f"{code} · {ordinal}" if code else ordinal,
                    year_from=year_from,
                    year_to=year_to,
                    body_types=[body] if body else [],
                    source_refs=[ref],
                )
                previous = generations.get(key)
                if previous is None:
                    generations[key] = candidate
                else:
                    merged = CatalogCache._merge_generation(
                        previous.model_dump(mode="json"), candidate.model_dump(mode="json")
                    )
                    generations[key] = VehicleGeneration.model_validate(merged)
        if generations:
            return list(generations.values())

        # Older catalog pages embed modification tables directly under headings.
        for heading in soup.find_all("h3"):
            link = heading.find("a", href=True)
            table = heading.find_next("table", class_="complectation-table")
            if not isinstance(link, Tag) or not isinstance(table, Tag):
                continue
            generation_url = urljoin(page_url, str(link.get("href")))
            if "/catalog/" not in generation_url:
                continue
            heading_text = heading.get_text(" ", strip=True)
            years = [int(value) for value in re.findall(r"(?:19|20)\d{2}", heading_text)]
            year_from = years[0] if years else None
            year_to = years[1] if len(years) > 1 else None
            body = cls._body_from_text(heading_text)
            modifications, body_codes = cls._parse_modification_table(table, generation_url, body)
            if not modifications:
                continue
            generation_slug = _source_ref("drom.ru", generation_url).slug
            code = sorted(body_codes)[0] if body_codes else None
            ordinal = re.search(r"(\d+\s+поколение(?:,\s*рестайлинг)?)", heading_text, re.I)
            ordinal_name = ordinal.group(1) if ordinal else heading_text.split(",", 1)[0]
            name = f"{code} · {ordinal_name}" if code else ordinal_name
            generation_key = (ordinal_name.casefold(), year_from)
            existing = generations.get(generation_key)
            if existing is None:
                generations[generation_key] = VehicleGeneration(
                    id=f"drom:{generation_slug}",
                    name=name,
                    year_from=year_from,
                    year_to=year_to,
                    body_types=[body] if body else [],
                    source_refs=[_source_ref("drom.ru", generation_url)],
                    modifications=modifications,
                )
            else:
                existing.year_to = (
                    None if None in (existing.year_to, year_to) else max(existing.year_to, year_to)
                )
                if body and body not in existing.body_types:
                    existing.body_types.append(body)
                existing.source_refs.append(_source_ref("drom.ru", generation_url))
                known = {item.id for item in existing.modifications}
                existing.modifications.extend(
                    item for item in modifications if item.id not in known
                )
        return list(generations.values())

    @classmethod
    def parse_drom_generation(
        cls, content: bytes, generation_url: str, body_types: list[BodyType]
    ) -> list[VehicleModification]:
        soup = BeautifulSoup(content, "lxml")
        by_id: dict[str, VehicleModification] = {}
        for table in soup.find_all("table"):
            if not table.find("a", href=re.compile(r"/\d+/?$")):
                continue
            modifications, _ = cls._parse_modification_table(
                table, generation_url, body_types[0] if len(body_types) == 1 else None
            )
            for modification in modifications:
                by_id[modification.id] = modification
        return list(by_id.values())

    @classmethod
    def _parse_modification_table(
        cls, table: Tag, generation_url: str, body: BodyType | None
    ) -> tuple[list[VehicleModification], set[str]]:
        current = EngineSpec()
        transmission = None
        drivetrain = None
        modifications: list[VehicleModification] = []
        body_codes: set[str] = set()
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) == 1 and (cells[0].name == "th" or int(cells[0].get("colspan", 1)) >= 3):
                spec = cells[0].get_text(" ", strip=True)
                current = cls._engine_from_text(spec)
                engine_link = cells[0].find("a", href=re.compile(r"/engine/"))
                if isinstance(engine_link, Tag):
                    current.engine_code = engine_link.get_text(" ", strip=True) or None
                transmission = Normalizer.transmission_from_text(spec)
                drivetrain = cls._drivetrain_from_text(spec)
                continue
            link = row.find("a", href=True)
            if not isinstance(link, Tag) or len(cells) < 5:
                continue
            url = urljoin(generation_url, str(link.get("href")))
            if not re.search(r"/\d+/$", urlparse(url).path):
                continue
            name = link.get_text(" ", strip=True)
            engine_code = current.engine_code or cells[3].get_text(" ", strip=True) or None
            body_code = cells[4].get_text(" ", strip=True)
            if body_code:
                body_codes.add(body_code)
            engine = current.model_copy(update={"engine_code": engine_code})
            slug = _source_ref("drom.ru", url).slug
            modifications.append(
                VehicleModification(
                    id=f"drom:{slug}",
                    name=name,
                    engine=engine,
                    transmission=transmission,
                    drivetrain=drivetrain,
                    body_type=body,
                    source_refs=[_source_ref("drom.ru", url)],
                )
            )
        return modifications, body_codes

    @staticmethod
    def _engine_from_text(text: str) -> EngineSpec:
        displacement = re.search(r"(\d+(?:[.,]\d+)?)\s*л\b", text, re.I)
        power = re.search(r"(\d+)\s*л\.\s*с\.", text, re.I)
        folded = text.casefold()
        fuels = (
            ("дизель", "дизель"),
            ("бензин", "бензин"),
            ("электро", "электро"),
            ("гибрид", "гибрид"),
            ("газ", "газ"),
        )
        fuel = next((value for token, value in fuels if token in folded), None)
        return EngineSpec(
            fuel_type=fuel,
            displacement_l=float(displacement.group(1).replace(",", ".")) if displacement else None,
            power_hp=int(power.group(1)) if power else None,
        )

    @staticmethod
    def _drivetrain_from_text(text: str) -> str | None:
        folded = text.casefold()
        if "полный" in folded or "4wd" in folded:
            return "awd"
        if "передн" in folded:
            return "fwd"
        if "задн" in folded:
            return "rwd"
        return None

    @staticmethod
    def _body_from_text(text: str) -> BodyType | None:
        folded = text.casefold()
        if "suv" in folded or "джип" in folded:
            return BodyType.SUV
        for source, body in (
            ("седан", BodyType.SEDAN),
            ("универсал", BodyType.WAGON),
            ("хэтчбек", BodyType.HATCHBACK),
            ("лифтбек", BodyType.LIFTBACK),
            ("купе", BodyType.COUPE),
            ("кабриолет", BodyType.CONVERTIBLE),
            ("пикап", BodyType.PICKUP),
            ("минивэн", BodyType.MINIVAN),
            ("фургон", BodyType.VAN),
        ):
            if source in folded:
                return body
        return None


async def _run() -> None:
    parser = argparse.ArgumentParser(description="Refresh the local marketplace catalog cache")
    parser.add_argument(
        "--source", choices=("cars-base.ru", "all", "auto.ru", "drom.ru"), default="cars-base.ru"
    )
    parser.add_argument(
        "--audit", action="store_true", help="CarsBase dry-run without catalog writes"
    )
    parser.add_argument("--force", action="store_true", help="Import CarsBase even when unchanged")
    args = parser.parse_args()
    if args.source == "cars-base.ru":
        from backend.services.carsbase_catalog import CarsBaseSyncService

        result = await CarsBaseSyncService().sync(force=args.force, dry_run=args.audit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] in {"error", "rate_limited"}:
            raise SystemExit(2)
        return
    if args.audit or args.force:
        parser.error("--audit and --force apply only to --source cars-base.ru")
    sources = {"auto.ru": auto_ru_source, "drom.ru": drom_source}
    selected = list(sources) if args.source == "all" else [args.source]
    status = await CatalogSyncService([sources[name]() for name in selected]).sync()
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_run())
