#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

uv sync
uv run python -m backend.database.init_db
uv run playwright install chromium
npm --prefix frontend install
npm --prefix frontend run build

echo "CarAnalyzer updated. Existing .env and SQLite knowledge data were preserved."
