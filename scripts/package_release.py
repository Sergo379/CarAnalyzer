import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    ".uv-cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "logs",
    "browser",
}
EXCLUDED_NAMES = {".env", "vehicle_catalog_runtime.json"}
EXCLUDED_SUFFIXES = {".db", ".pyc", ".tmp", ".log"}


def include(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return not (
        any(part in EXCLUDED_PARTS for part in relative.parts)
        or path.name in EXCLUDED_NAMES
        or path.suffix in EXCLUDED_SUFFIXES
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a clean CarAnalyzer release archive")
    parser.add_argument("output", nargs="?", default="dist/caranalyzer-release.zip")
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file() and path != output and include(path):
                archive.write(path, path.relative_to(ROOT))
    print(output)


if __name__ == "__main__":
    main()
