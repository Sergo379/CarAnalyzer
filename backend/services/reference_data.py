import json
from functools import lru_cache
from typing import Any

from backend.config import PROJECT_ROOT

DATA_ROOT = PROJECT_ROOT / "backend" / "data"


def _read_json(name: str) -> dict[str, Any]:
    path = DATA_ROOT / name
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache
def vehicle_catalog() -> dict[str, Any]:
    return _read_json("vehicle_catalog.json")


@lru_cache
def regions_catalog() -> dict[str, Any]:
    return _read_json("regions.json")
