import argparse
import asyncio
import json
import time

from backend.models.car import SearchRequest
from backend.services.search_service import SearchService

CASES = {
    "fiesta": dict(brand="Ford", model="Fiesta", year=2016, body_type="hatchback", price=700_000),
    "bmw5": dict(brand="BMW", model="5-Series", year=2022, body_type="sedan", price=4_100_000),
    "xray": dict(brand="Lada", model="XRAY", year=2022, body_type="hatchback", price=700_000),
    "a5": dict(
        brand="Audi", model="A5", year=2019, body_type="liftback", region="moscow", price=3_200_000
    ),
    "camry": dict(brand="Toyota", model="Camry", year=2020, body_type="sedan", price=2_800_000),
    "x5": dict(brand="BMW", model="X5", year=2018, body_type="suv", price=5_000_000),
    "range_rover": dict(
        brand="Land Rover", model="Range Rover", year=2020, body_type="suv", price=8_000_000
    ),
}


async def run(selected: list[str], delay: float) -> None:
    rows = []
    for index, name in enumerate(selected):
        started = time.perf_counter()
        result = await SearchService().search(SearchRequest(**CASES[name], debug=True))
        rows.append(
            {
                "case": name,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "operations": {
                    source: {
                        operation: data.model_dump(mode="json")
                        for operation, data in values.items()
                    }
                    for source, values in result.source_operations.items()
                },
                "source_market": result.source_distribution,
                "direct": len(result.direct.listings),
                "expensive": len(result.expensive.listings),
                "cheaper": len(result.cheaper.listings),
                "pipeline": result.pipeline_diagnostics,
                "urls": {
                    source: diagnostic.resolved_urls
                    for source, diagnostic in result.source_diagnostics.items()
                },
            }
        )
        if index + 1 < len(selected):
            await asyncio.sleep(delay)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Low-frequency live marketplace acceptance smoke")
    parser.add_argument("--case", action="append", choices=sorted(CASES))
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()
    asyncio.run(run(args.case or list(CASES), max(args.delay, 0)))


if __name__ == "__main__":
    main()
