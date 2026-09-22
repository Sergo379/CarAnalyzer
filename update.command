#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [[ -d .git ]]; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "Update stopped: commit or stash local source changes first. Runtime data is untouched."
    exit 1
  fi
  git fetch --prune
  git pull --ff-only
fi

if [[ -f data/car_analyzer.db ]]; then
  cp -p data/car_analyzer.db "data/car_analyzer.db.backup-$(date +%Y%m%d-%H%M%S)"
fi

uv sync
uv run python -m backend.database.init_db
uv run playwright install chromium
npm --prefix frontend ci
npm --prefix frontend run build

echo "CarAnalyzer updated. Existing .env and SQLite knowledge data were preserved."
