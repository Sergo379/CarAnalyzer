import sqlite3

from backend.database.init_db import initialize_connection


def test_database_initialization_is_idempotent() -> None:
    with sqlite3.connect(":memory:") as connection:
        initialize_connection(connection)
        connection.execute(
            "INSERT INTO searches "
            "(brand, model, year, body_type, price, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("BMW", "520i", 2022, "sedan", 4_100_000, "2026-09-22T00:00:00Z"),
        )
        connection.commit()
        initialize_connection(connection)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        count = connection.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
    assert {"listings", "listing_price_history", "car_models", "car_problems", "searches"} <= tables
    assert count == 1
