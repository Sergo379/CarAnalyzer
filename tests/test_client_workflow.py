"""Synthetic client-data and deterministic macOS script checks."""

import sqlite3
from pathlib import Path

import pytest

from backend.tools.client_db_backup import backup_databases

ROOT = Path(__file__).resolve().parents[1]


def script(name: str) -> str:
    raw = (ROOT / name).read_bytes()
    assert raw.startswith(b"#!/bin/bash\n")
    assert b"\r\n" not in raw
    return raw.decode("utf-8")


def test_sqlite_backup_is_consistent_and_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "client.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES ('original')")

    [backup] = backup_databases([source], timestamp="20261005-000000")
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE records SET value = 'newer'")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == ("original",)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    with pytest.raises(FileExistsError):
        backup_databases([source], timestamp="20261005-000000")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == ("original",)


def test_install_script_keeps_existing_secrets_and_checks_health() -> None:
    source = script("install.command")
    assert 'if [[ ! -f .env ]]' in source
    assert "cp .env.example .env" in source
    assert "uv python install 3.13" in source
    assert "uv sync --locked --extra knowledge --no-dev" in source
    assert "playwright install chromium" in source
    assert "npm --prefix frontend ci" in source
    assert "npm --prefix frontend run build" in source
    assert "backend.database.init_db" in source
    assert source.index("backend.tools.client_db_backup") < source.index("backend.database.init_db")
    assert "--source cars-base.ru" in source
    assert "http://127.0.0.1:8000/health" in source


def test_start_script_has_foreground_lifecycle_and_port_guard() -> None:
    source = script("start.command")
    assert "iTCP:8000 -sTCP:LISTEN" in source
    assert "--source cars-base.ru" in source
    assert "--workers 1" in source
    assert "trap cleanup EXIT" in source
    assert "trap 'exit 130' INT" in source
    assert "trap 'exit 129' HUP" in source
    assert 'kill -TERM "$SERVER_PID"' in source
    assert 'wait "$SERVER_PID"' in source
    assert source.index("/health") < source.index("open http://127.0.0.1:8000")
    assert "nohup" not in source
    assert "--reload" not in source


def test_update_script_refuses_dirty_source_and_backs_up_before_migration() -> None:
    source = script("update.command")
    assert "[[ -d .git ]]" in source
    assert "git status --porcelain --untracked-files=normal" in source
    assert "git fetch --prune" in source
    assert "git pull --ff-only" in source
    assert source.index("backend.tools.client_db_backup") < source.index("git pull --ff-only")
    assert source.index("backend.tools.client_db_backup") < source.index("backend.database.init_db")
    assert "uv sync --locked --extra knowledge --no-dev" in source
    assert "npm --prefix frontend ci" in source
    assert "git reset" not in source
    assert "git checkout" not in source
