"""Bounded, exact Drom model mapping for a single selected canonical model."""

from __future__ import annotations

import asyncio
from time import monotonic

import httpx

from backend.services.marketplace_catalog import (
    CatalogCache,
    _brand_name,
    _identity_keys,
    _key,
    _source_ref,
    drom_source,
)

_MODEL_LOCKS: dict[str, asyncio.Lock] = {}
_RECENT_FAILURES: dict[str, tuple[str, float]] = {}


class DromModelResolver:
    def __init__(self, cache: CatalogCache):
        self.cache = cache

    async def resolve(self, brand: str, model: str, client: httpx.AsyncClient | None = None) -> str:
        """Return mapped, ambiguous, rate_limited, source_unavailable, or not_found."""
        identity = self.cache.resolve_identity(brand, model)
        if identity.provisional:
            return "not_found"
        if self.cache.source_model_ref("drom.ru", identity.brand, identity.model):
            return "mapped"
        key = f"{self.cache.write_path()}|{identity.canonical_model_id}"
        lock = _MODEL_LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            if self.cache.source_model_ref("drom.ru", identity.brand, identity.model):
                _RECENT_FAILURES.pop(key, None)
                return "mapped"
            recent = _RECENT_FAILURES.get(key)
            if recent and monotonic() < recent[1]:
                return recent[0]
            if client is None:
                async with httpx.AsyncClient(
                    timeout=12, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0"}
                ) as owned:
                    result = await self._bounded_resolve(identity.brand, identity.model, owned)
            else:
                result = await self._bounded_resolve(identity.brand, identity.model, client)
            if result in {"rate_limited", "source_unavailable"}:
                _RECENT_FAILURES[key] = (
                    result,
                    monotonic() + (120 if result == "rate_limited" else 15),
                )
            return result

    async def _bounded_resolve(self, brand: str, model: str, client: httpx.AsyncClient) -> str:
        try:
            return await asyncio.wait_for(self._resolve(brand, model, client), timeout=25)
        except TimeoutError:
            return "source_unavailable"

    async def _resolve(self, brand: str, model: str, client: httpx.AsyncClient) -> str:
        source = drom_source()
        stored_brand = self.cache.brand_entry(brand) or {}
        brand_refs = [
            ref for ref in stored_brand.get("source_refs", []) if ref.get("source") == "drom.ru"
        ]
        try:
            if len(brand_refs) == 1:
                brand_name, brand_url = brand, str(brand_refs[0]["url"])
            else:
                index = await client.get(source.index_url)
                if index.status_code == 429:
                    return "rate_limited"
                if index.status_code in {403, 503}:
                    return "source_unavailable"
                index.raise_for_status()
                brand_key = _key(_brand_name(brand))
                matches = [
                    (name, url)
                    for name, url in source._brand_links(index.content).items()
                    if _key(_brand_name(name)) == brand_key
                ]
                if len(matches) != 1:
                    return "ambiguous" if matches else "not_found"
                brand_name, brand_url = matches[0]
            page = await client.get(brand_url)
            if page.status_code == 429:
                return "rate_limited"
            if page.status_code in {403, 503}:
                return "source_unavailable"
            page.raise_for_status()
            wanted = _identity_keys(model)
            matches = [
                candidate
                for candidate in source._models(page.content, brand_url, brand_name)
                if wanted
                & (
                    _identity_keys(candidate.name)
                    | _identity_keys(candidate.url.rstrip("/").rsplit("/", 1)[-1])
                )
            ]
            if len(matches) != 1:
                return "ambiguous" if matches else "not_found"
            await self.cache.merge_source_mapping(
                "drom.ru",
                brand,
                model,
                _source_ref("drom.ru", brand_url),
                _source_ref("drom.ru", matches[0].url),
            )
            return "mapped"
        except httpx.HTTPError:
            return "source_unavailable"
