#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

fail() { echo "Installation failed: $*" >&2; exit 1; }
trap 'echo "Installation failed. See the command output; server details are in logs/install-health.log if it started." >&2' ERR

[[ "$(uname -s)" == "Darwin" ]] || fail "install.command requires macOS."
[[ "$(uname -m)" == "arm64" ]] || fail "This client package targets Apple Silicon (arm64)."
[[ -f uv.lock && -f frontend/package-lock.json && -f .env.example ]] ||
  fail "The release is incomplete: uv.lock, frontend/package-lock.json or .env.example is missing."

if ! command -v uv >/dev/null 2>&1 || ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  if ! command -v brew >/dev/null 2>&1; then
    fail "Install Homebrew using https://brew.sh, then run install.command again."
  fi
  if ! command -v uv >/dev/null 2>&1; then brew install uv; fi
  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then brew install node; fi
fi

node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit((major === 20 && minor >= 19) || (major === 22 && minor >= 12) || major > 22 ? 0 : 1)' ||
  fail "Node.js must be 20.19+ or 22.12+; update Node.js, then run install.command again."
command -v curl >/dev/null 2>&1 || fail "curl is missing; install macOS Command Line Tools, then retry."
command -v lsof >/dev/null 2>&1 || fail "lsof is missing; install macOS Command Line Tools, then retry."

echo "Installing Python 3.13 and locked application dependencies..."
uv python install 3.13
uv sync --locked --extra knowledge --no-dev
.venv/bin/python -m playwright install chromium

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from the example; no API key was added."
fi

.venv/bin/python -m backend.tools.client_db_backup
.venv/bin/python -m backend.database.init_db
npm --prefix frontend ci
npm --prefix frontend run build

echo "Checking CarsBase inventory; an unavailable source will not erase the local catalog..."
if ! .venv/bin/python -m backend.services.marketplace_catalog --source cars-base.ru; then
  echo "CarsBase is unavailable. The local catalog remains usable; retry on the next start."
fi

mkdir -p logs
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  fail "Port 8000 is already in use. Stop the other server before the installation health check."
fi

.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 \
  --workers 1 --timeout-graceful-shutdown 10 >logs/install-health.log 2>&1 &
SERVER_PID=$!
cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill -TERM "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

for _ in {1..30}; do
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    fail "Backend exited during the health check. See logs/install-health.log."
  fi
  if curl --fail --silent --max-time 2 http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "CarAnalyzer installed successfully. Use start.command for normal operation."
    exit 0
  fi
  sleep 1
done

fail "Health check timed out. See logs/install-health.log."
