#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

fail() { echo "Update stopped: $*" >&2; exit 1; }
trap 'echo "Update failed. Existing runtime data and SQLite backups were preserved." >&2' ERR

[[ "$(uname -s)" == "Darwin" ]] || fail "update.command requires macOS."
[[ -d .git ]] || fail "the .git directory is missing; use a clean Git clone for updates."
[[ -x .venv/bin/python ]] || fail "installation is incomplete; run install.command first."
command -v git >/dev/null 2>&1 || fail "Git is missing; install Xcode Command Line Tools, then retry."
command -v uv >/dev/null 2>&1 || fail "uv is missing; run install.command first."
command -v npm >/dev/null 2>&1 || fail "Node.js/npm is missing; run install.command first."
command -v lsof >/dev/null 2>&1 || fail "lsof is missing; install macOS Command Line Tools, then retry."
git symbolic-ref --quiet --short HEAD >/dev/null ||
  fail "detached HEAD; switch to the designated stable branch manually before updating."
git rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1 ||
  fail "the current branch has no upstream; configure the designated stable branch first."
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  fail "local source files have changed. Save/review them before updating; runtime data was not touched."
fi
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  fail "port 8000 is in use. Close the start.command Terminal window before updating."
fi

echo "Backing up SQLite databases before migration..."
.venv/bin/python -m backend.tools.client_db_backup

git fetch --prune
git pull --ff-only

uv python install 3.13
uv sync --locked --extra knowledge --no-dev
.venv/bin/python -m backend.database.init_db
.venv/bin/python -m playwright install chromium
npm --prefix frontend ci
npm --prefix frontend run build

echo "CarAnalyzer updated. The .env, runtime catalog, SQLite data and browser runtime were preserved."
echo "Run start.command to start the updated application."
