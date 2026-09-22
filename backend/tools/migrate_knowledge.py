import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from backend.config import get_settings
from backend.database.init_db import initialize_connection


def migrate(path: Path) -> tuple[Path, int]:
    if not path.exists():
        with sqlite3.connect(path) as connection:
            initialize_connection(connection)
        return path, 0
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.backup-{stamp}")
    shutil.copy2(path, backup)
    with sqlite3.connect(path) as connection:
        initialize_connection(connection)
        cursor = connection.execute(
            """
            DELETE FROM car_profiles
            WHERE technical_summary IS NULL
              AND knowledge_updated_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM car_problem_profiles problems
                WHERE problems.car_profile_id = car_profiles.id
              )
              AND NOT EXISTS (
                SELECT 1 FROM rag_documents documents
                WHERE documents.car_profile_id = car_profiles.id
              )
            """
        )
        removed = cursor.rowcount
        connection.commit()
    return backup, removed


if __name__ == "__main__":
    backup_path, removed_count = migrate(get_settings().resolved_database_path())
    print(f"backup={backup_path} removed_empty_profiles={removed_count}")
