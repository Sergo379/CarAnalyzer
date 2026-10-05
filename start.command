#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "start.command requires macOS." >&2
  exit 1
fi
if [[ ! -x .venv/bin/python || ! -f frontend/dist/index.html ]]; then
  echo "Installation is incomplete. Run install.command first." >&2
  exit 1
fi
if ! command -v lsof >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  echo "macOS networking tools are missing. Install Command Line Tools, then retry." >&2
  exit 1
fi
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8000 is already in use. Close the existing CarAnalyzer window or stop the other server." >&2
  exit 1
fi

mkdir -p logs
echo "Checking CarsBase for a newer brand/model inventory..."
if ! .venv/bin/python -m backend.services.marketplace_catalog --source cars-base.ru; then
  echo "CarsBase is unavailable; continuing with the last-good local catalog."
fi

.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 \
  --workers 1 --timeout-graceful-shutdown 10 >>logs/backend.log 2>&1 &
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
    echo "Backend stopped unexpectedly. See logs/backend.log."
    exit 1
  fi
  if curl --fail --silent --max-time 2 http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "CarAnalyzer is running at http://127.0.0.1:8000. Close this Terminal window or press Ctrl+C to stop it."
    echo "Backend log: logs/backend.log"
    open http://127.0.0.1:8000 || echo "Open http://127.0.0.1:8000 in your browser."
    wait "$SERVER_PID"
    exit $?
  fi
  sleep 1
done

echo "Backend did not become ready. See logs/backend.log."
exit 1
