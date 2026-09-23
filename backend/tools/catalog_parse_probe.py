"""Read-only structure probe for currently failed Drom model pages."""

from __future__ import annotations

import asyncio
import json

import httpx
from bs4 import BeautifulSoup

from backend.services.marketplace_catalog import CatalogCache, CatalogEnrichmentService


async def main() -> None:
    cache = CatalogCache()
    failures = [
        (brand["name"], model["name"], ref["url"])
        for brand in cache.load()["brands"]
        for model in brand["models"]
        if model.get("details_status") == "parse_error"
        for ref in model["source_refs"]
        if ref["source"] == "drom.ru"
    ]
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=25
    ) as client:
        for brand, model, url in failures:
            response = await client.get(url)
            soup = BeautifulSoup(response.content, "lxml")
            markers = soup.select('[data-ga-stats-name="generations_outlet_item"]')
            parsed = CatalogEnrichmentService.parse_drom_year(
                response.content, brand, model, 0, url
            )
            print(
                json.dumps(
                    {
                        "brand": brand,
                        "model": model,
                        "status": response.status_code,
                        "url": str(response.url),
                        "markers": len(markers),
                        "parsed_generations": len(parsed),
                        "headings": [
                            tag.get_text(" ", strip=True)[:100]
                            for tag in soup.select("h1,h2,h3")[:8]
                        ],
                        "marker_links": [
                            (anchor.get_text(" ", strip=True)[:100], anchor.get("href"))
                            for marker in markers[:5]
                            for anchor in marker.select("a[href]")[:1]
                        ],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
