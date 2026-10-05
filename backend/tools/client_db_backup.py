"""Consistent, non-destructive SQLite backups for the client updater."""

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from backend.config import PROJECT_ROOT, get_settings


def runtime_databases() -> list[Path]:
    configured = get_settings().resolved_database_path()
    candidates = {configured, *(PROJECT_ROOT / "data").glob("*.db")}
    return sorted((path for path in candidates if path.is_file()), key=str)


def backup_databases(
    paths: Iterable[Path] | None = None, *, timestamp: str | None = None
) -> list[Path]:
    stamp = timestamp or datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    backups: list[Path] = []
    for source in paths if paths is not None else runtime_databases():
        if not source.is_file():
            continue
        directory = source.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{source.stem}-{stamp}.db"
        if target.exists():
            raise FileExistsError(f"SQLite backup already exists: {target}")
        try:
            with closing(sqlite3.connect(source)) as original, closing(
                sqlite3.connect(target)
            ) as destination:
                original.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError(f"SQLite backup failed integrity check: {source}")
            backups.append(target)
        except Exception:
            target.unlink(missing_ok=True)
            raise
    return backups


if __name__ == "__main__":
    copied = backup_databases()
    for path in copied:
        print(f"SQLite backup: {path}")
    if not copied:
        print("No existing SQLite database to back up; migration will initialize a new one.")
