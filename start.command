#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000 >logs/backend.log 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for _ in {1..30}; do
  if curl --fail --silent http://127.0.0.1:8000/health >/dev/null; then
    open http://127.0.0.1:8000
    wait "$SERVER_PID"
    exit $?
  fi
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    echo "Backend stopped unexpectedly. See logs/backend.log."
    exit 1
  fi
  sleep 1
done

echo "Backend did not become ready. See logs/backend.log."
exit 1
