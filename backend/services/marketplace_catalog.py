from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

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

CATALOG_PATH = DATA_ROOT / "vehicle_catalog.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
DETAIL_TTL = timedelta(days=90)


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
            headers={"User-Agent": USER_AGENT},
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
    normalized = value.casefold().replace("серии", "series").replace("серия", "series")
    key = _key(normalized)
    match = re.fullmatch(r"(\d+)er", key)
    return f"{match.group(1)}series" if match else key


def _near_alias(left: str, right: str) -> bool:
    """Conservative one-edit fallback for cross-source spellings of longer model names."""
    if left == right:
        return True
    if min(len(left), len(right)) < 5 or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) == 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    index = 0
    while index < len(shorter) and shorter[index] == longer[index]:
        index += 1
    return shorter[index:] == longer[index + 1 :]


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
    path: Path = CATALOG_PATH
    _payload: dict[str, object] | None = field(default=None, init=False)
    _mtime_ns: int | None = field(default=None, init=False)

    def load(self) -> dict[str, object]:
        mtime_ns = self.path.stat().st_mtime_ns
        if self._payload is None or self._mtime_ns != mtime_ns:
            self._payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._mtime_ns = mtime_ns
        return self._payload

    def brands(self) -> list[str]:
        return [entry["name"] for entry in self.load().get("brands", [])]  # type: ignore[index]

    def model_entries(self, brand: str) -> list[dict[str, object]]:
        entry = self._brand_entry(brand)
        return list(entry.get("models", [])) if entry else []  # type: ignore[arg-type]

    def models(self, brand: str) -> list[str]:
        return [str(model["name"]) for model in self.model_entries(brand)]

    def model_entry(self, brand: str, model: str) -> dict[str, object] | None:
        needle = _model_key(model)
        for entry in self.model_entries(brand):
            aliases = entry.get("aliases", [])
            keys = {_model_key(str(entry["name"])), *(_model_key(str(x)) for x in aliases)}
            if needle in keys:
                return entry
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

        entries = list(brand_entry.get("models", []))
        needle = _model_key(model)
        match = self.model_entry(str(brand_entry["name"]), model)
        if match is None:
            candidates: list[tuple[int, dict[str, object]]] = []
            for entry in entries:
                names = [str(entry["name"]), *(str(x) for x in entry.get("aliases", []))]
                for value in names:
                    key = _model_key(value)
                    if key and (key in needle or needle in key):
                        candidates.append((len(key), entry))
                for raw_ref in entry.get("source_refs", []):
                    ref_key = _model_key(str(raw_ref.get("slug", "")))
                    if ref_key and (ref_key in needle or needle in ref_key):
                        candidates.append((len(ref_key), entry))
            # A three-digit trim such as 520i -> 5 Series is a catalog-derived
            # fallback, not a make-specific rule.
            numeric = re.match(r"([1-9])\d{2}[a-z]*$", needle)
            if numeric:
                ordinal = numeric.group(1)
                for entry in entries:
                    key = _model_key(str(entry["name"]))
                    if key.startswith(ordinal) and any(
                        marker in key for marker in ("series", "серии", "serie", "er")
                    ):
                        candidates.append((10_000 - len(key), entry))
            if candidates:
                match = max(candidates, key=lambda item: item[0])[1]

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
        }

    def save(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)
        self._payload = payload
        self._mtime_ns = self.path.stat().st_mtime_ns

    def save_model_details(
        self, brand: str, model: str, generations: list[VehicleGeneration]
    ) -> None:
        payload = self.load()
        entry = self.model_entry(brand, model)
        if entry is None:
            return
        existing = {generation["id"]: generation for generation in entry.get("generations", [])}
        for generation in generations:
            existing[generation.id] = generation.model_dump(mode="json")
        entry["generations"] = list(existing.values())
        entry["details_updated_at"] = datetime.now(UTC).isoformat()
        self.save(payload)

    def details_are_fresh(self, brand: str, model: str, year: int) -> bool:
        entry = self.model_entry(brand, model)
        if not entry or not self.generations(brand, model, year):
            return False
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
                    key = next(
                        (known for known in models if _near_alias(known, key)),
                        key,
                    )
                    model = models.setdefault(
                        key,
                        CatalogModel(id=f"{brand_key}:{key}", name=model_name, source_refs=[]),
                    )
                    if model_name != model.name and model_name not in model.aliases:
                        model.aliases.append(model_name)
                    model.source_refs.append(_source_ref(result.source, source_model.url))

        for brand_key, brand in merged.items():
            brand.models = sorted(model_maps[brand_key].values(), key=lambda x: x.name.casefold())
        brands = sorted(merged.values(), key=lambda x: x.name.casefold())
        payload: dict[str, object] = {
            "version": 3,
            "catalog_updated_at": datetime.now(UTC).isoformat(),
            "sources": source_status,
            "brands": [brand.model_dump(mode="json") for brand in brands],
        }
        self.cache.save(payload)
        return self.cache.status()


class CatalogEnrichmentService:
    def __init__(self, cache: CatalogCache | None = None) -> None:
        self.cache = cache or CatalogCache()

    async def ensure(self, brand: str, model: str, year: int) -> None:
        if self.cache.details_are_fresh(brand, model, year):
            return
        ref = self.cache.source_model_ref("drom.ru", brand, model)
        if ref is None:
            return
        url = ref.url.rstrip("/") + f"/{year}/"
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        generations = self.parse_drom_year(response.content, brand, model, year, url)
        if generations:
            self.cache.save_model_details(brand, model, generations)

    @classmethod
    def parse_drom_year(
        cls, content: bytes, brand: str, model: str, year: int, page_url: str
    ) -> list[VehicleGeneration]:
        soup = BeautifulSoup(content, "lxml")
        generations: dict[tuple[str, int], VehicleGeneration] = {}
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
            year_from = years[0] if years else year
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
                existing.year_to = max(
                    (value for value in (existing.year_to, year_to) if value is not None),
                    default=None,
                )
                existing.source_refs.append(_source_ref("drom.ru", generation_url))
                known = {item.id for item in existing.modifications}
                existing.modifications.extend(
                    item for item in modifications if item.id not in known
                )
        return list(generations.values())

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
            if len(cells) == 1 and cells[0].name == "th":
                spec = cells[0].get_text(" ", strip=True)
                current = cls._engine_from_text(spec)
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
            engine_code = cells[3].get_text(" ", strip=True) or None
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
    parser.add_argument("--source", choices=("all", "auto.ru", "drom.ru"), default="all")
    args = parser.parse_args()
    sources = {"auto.ru": auto_ru_source, "drom.ru": drom_source}
    selected = list(sources) if args.source == "all" else [args.source]
    status = await CatalogSyncService([sources[name]() for name in selected]).sync()
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_run())
