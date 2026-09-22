import json
from functools import lru_cache
from typing import Any

from backend.config import PROJECT_ROOT

DATA_ROOT = PROJECT_ROOT / "backend" / "data"


def _read_json(name: str) -> dict[str, Any]:
    path = DATA_ROOT / name
    return json.loads(path.read_text(encoding="utf-8"))


def vehicle_catalog() -> dict[str, Any]:
    from backend.services.marketplace_catalog import CatalogCache

    payload = CatalogCache().load()
    # Preserve the legacy full-catalog endpoint while the primary API serves
    # brands and models independently. Version 2 stores model provenance.
    if payload.get("version") in {2, 3}:
        return {
            **payload,
            "brands": [
                {
                    "name": entry["name"],
                    "models": [
                        model["name"] if isinstance(model, dict) else model
                        for model in entry["models"]
                    ],
                }
                for entry in payload["brands"]
            ],
        }
    return payload


@lru_cache
def regions_catalog() -> dict[str, Any]:
    return _read_json("regions.json")
