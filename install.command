#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer is intended for macOS."
  exit 1
fi

ARCH="$(uname -m)"
if [[ "$ARCH" != "arm64" ]]; then
  echo "Warning: expected Apple Silicon arm64, detected $ARCH."
fi

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required to install missing uv/Node packages safely."
  echo "Install it from https://brew.sh and run install.command again."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  brew install uv
fi
if ! command -v node >/dev/null 2>&1; then
  brew install node
fi

uv python install 3.13
uv sync
uv run playwright install chromium

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

uv run python -m backend.database.init_db
npm --prefix frontend install
npm --prefix frontend run build

mkdir -p logs
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000 >logs/install-health.log 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for _ in {1..30}; do
  if curl --fail --silent http://127.0.0.1:8000/health >/dev/null; then
    echo "CarAnalyzer installed successfully."
    exit 0
  fi
  sleep 1
done

echo "Health check failed. See logs/install-health.log."
exit 1
