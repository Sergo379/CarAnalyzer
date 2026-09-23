"""Read-only public brand/model index audit; never fetch vehicle detail pages."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from backend.services.marketplace_catalog import (
    CatalogCache,
    PublicHtmlCatalogSource,
    _brand_name,
    _identity_keys,
    _key,
    auto_ru_source,
    drom_source,
)
from backend.tools.catalog_audit import PSEUDO_NAMES, audit_catalog


def _next_index_page(html: bytes, current_url: str, brand_url: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.select("a[href]"):
        label = anchor.get_text(" ", strip=True).casefold()
        rel = [str(value).casefold() for value in anchor.get("rel", [])]
        if "next" not in rel and label not in {"следующая", "далее", "next", "→"}:
            continue
        next_url = urljoin(current_url, str(anchor["href"]))
        if (
            urlparse(next_url).netloc == urlparse(brand_url).netloc
            and urlparse(next_url).path.startswith(urlparse(brand_url).path)
            and next_url != current_url
        ):
            return next_url
    return None


async def inspect_source(
    source: PublicHtmlCatalogSource,
    *,
    concurrency: int = 2,
    max_pages_per_brand: int = 10,
    max_brands: int | None = None,
) -> dict:
    semaphore = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency)
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
        timeout=source.timeout,
        limits=limits,
    ) as client:
        brand_links = source._brand_links(await source._get(client, source.index_url))
        unique_links = list(dict.fromkeys(brand_links.values()))
        if max_brands is not None:
            unique_links = unique_links[:max_brands]
        print(json.dumps({"source": source.name, "brand_pages": len(unique_links)}), flush=True)

        async def inspect_brand(url: str) -> dict:
            async with semaphore:
                names = [name for name, link in brand_links.items() if link == url]
                name = names[0]
                models: dict[str, dict[str, str]] = {}
                page_url: str | None = url
                seen_pages: set[str] = set()
                pseudo_names: list[str] = []
                try:
                    while (
                        page_url
                        and page_url not in seen_pages
                        and len(seen_pages) < max_pages_per_brand
                    ):
                        seen_pages.add(page_url)
                        content = await source._get(client, page_url)
                        for model in source._models(content, url, name):
                            if model.name.casefold() in PSEUDO_NAMES:
                                pseudo_names.append(model.name)
                            else:
                                models.setdefault(model.url, {"name": model.name, "url": model.url})
                        page_url = _next_index_page(content, page_url, url)
                    return {
                        "brand": name,
                        "brand_id": _key(_brand_name(name)),
                        "brand_url": url,
                        "models": list(models.values()),
                        "pages": len(seen_pages),
                        "pagination_truncated": bool(page_url and page_url not in seen_pages),
                        "pseudo_names": sorted(set(pseudo_names)),
                    }
                except (httpx.HTTPError, ValueError) as exc:
                    return {
                        "brand": name,
                        "brand_id": _key(_brand_name(name)),
                        "brand_url": url,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        brands = []
        for offset in range(0, len(unique_links), 20):
            brands.extend(
                await asyncio.gather(
                    *(inspect_brand(url) for url in unique_links[offset : offset + 20])
                )
            )
            print(
                json.dumps(
                    {
                        "source": source.name,
                        "pages_checked": min(offset + 20, len(unique_links)),
                        "page_errors": sum("error" in brand for brand in brands),
                    }
                ),
                flush=True,
            )
    return {"source": source.name, "brands": brands}


def compare_index(snapshot: dict, cache: CatalogCache | None = None) -> dict:
    cache = cache or CatalogCache()
    source = snapshot["source"]
    source_config = drom_source() if source == "drom.ru" else auto_ru_source()
    stored_payload = cache.load()
    stored_names = {
        str(brand["id"]): {
            key
            for model in brand.get("models", [])
            for name in [model["name"], *model.get("aliases", [])]
            for key in _identity_keys(str(name))
        }
        for brand in stored_payload.get("brands", [])
    }
    counts: dict[str, int] = {}
    source_models: dict[tuple[str, str], dict] = {}
    errors = []
    for brand in snapshot["brands"]:
        if brand["brand"].casefold() in source_config.excluded_brand_names:
            continue
        if "error" in brand:
            errors.append({"brand": brand["brand"], "error": brand["error"]})
            continue
        brand_id = brand["brand_id"]
        models = [
            model
            for model in brand["models"]
            if urlparse(model["url"]).path.rstrip("/").split("/")[-1].casefold()
            not in source_config.excluded_model_slugs
        ]
        counts[brand_id] = counts.get(brand_id, 0) + len(models)
        for model in models:
            source_models[(brand_id, model["url"])] = {
                "brand": brand["brand"],
                "brand_id": brand_id,
                **model,
            }
    stored = {
        (str(brand["id"]), str(ref["url"]))
        for brand in stored_payload.get("brands", [])
        for model in brand.get("models", [])
        for ref in model.get("source_refs", [])
        if ref.get("source") == source
    }
    missing = [model for key, model in source_models.items() if key not in stored]
    missing_identities = [
        model
        for (brand_id, _), model in source_models.items()
        if not _identity_keys(model["name"]) & stored_names.get(brand_id, set())
    ]
    completeness = audit_catalog(cache, {source: counts})
    return {
        "source": source,
        "source_brands_checked": len(counts),
        "source_models_found": len(source_models),
        "missing_source_refs": missing,
        "missing_source_ref_count": len(missing),
        "missing_model_identities": missing_identities,
        "missing_model_identity_count": len(missing_identities),
        "page_errors": errors,
        "pagination_brands": [
            brand["brand"] for brand in snapshot["brands"] if brand.get("pages", 0) > 1
        ],
        "pagination_truncated": [
            brand["brand"] for brand in snapshot["brands"] if brand.get("pagination_truncated")
        ],
        "pseudo_models": {
            brand["brand"]: brand["pseudo_names"]
            for brand in snapshot["brands"]
            if brand.get("pseudo_names")
        },
        "count_gap_summary": {
            key: completeness["summary"][key]
            for key in (
                "source_index_comparisons",
                "source_index_brands_with_gaps",
                "source_index_missing_mappings",
            )
        },
        "count_gap_details": [
            error
            for error in completeness["errors"]
            if error["type"] in {"source_model_count_gap", "source_brand_missing"}
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("drom.ru", "auto.ru"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-brands", type=int, help="Limit brand pages for a small check")
    args = parser.parse_args()
    if args.concurrency < 1 or args.concurrency > 4:
        parser.error("--concurrency must be between 1 and 4")
    source = drom_source() if args.source == "drom.ru" else auto_ru_source()
    snapshot = asyncio.run(
        inspect_source(source, concurrency=args.concurrency, max_brands=args.max_brands)
    )
    report = compare_index(snapshot)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"snapshot": snapshot, "report": report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"missing_source_refs", "missing_model_identities", "page_errors"}
            },
            ensure_ascii=False,
        )
    )
    print(
        json.dumps(
            {
                "missing_source_ref_count": len(report["missing_source_refs"]),
                "page_errors": len(report["page_errors"]),
            }
        )
    )


if __name__ == "__main__":
    main()
