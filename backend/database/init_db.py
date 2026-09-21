import sqlite3
from pathlib import Path

from backend.database.db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, external_id TEXT NOT NULL,
 brand TEXT NOT NULL, model TEXT NOT NULL, year INTEGER NOT NULL, body_type TEXT NOT NULL,
 price INTEGER NOT NULL CHECK (price > 0), url TEXT NOT NULL, location TEXT,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
 UNIQUE (source, external_id)
);
CREATE TABLE IF NOT EXISTS listing_price_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE RESTRICT,
 price INTEGER NOT NULL CHECK (price > 0), checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_listing_checked
ON listing_price_history(listing_id, checked_at);
CREATE TABLE IF NOT EXISTS car_models (
 id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT NOT NULL, model TEXT NOT NULL,
 generation TEXT, segment TEXT, body_types TEXT NOT NULL DEFAULT '[]',
 UNIQUE (brand, model, generation)
);
CREATE TABLE IF NOT EXISTS car_problems (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 car_model_id INTEGER NOT NULL REFERENCES car_models(id) ON DELETE RESTRICT,
 problem TEXT NOT NULL, description TEXT NOT NULL, severity TEXT, source TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS searches (
 id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT NOT NULL, model TEXT NOT NULL,
 year INTEGER NOT NULL, body_type TEXT NOT NULL, price INTEGER NOT NULL CHECK (price > 0),
 created_at TEXT NOT NULL
);
"""


def initialize_connection(connection: sqlite3.Connection) -> None:
    """Apply the non-destructive schema to an open SQLite connection."""
    connection.executescript(SCHEMA)
    connection.commit()


def initialize_database(path: Path | None = None) -> Path:
    with connect(path) as connection:
        initialize_connection(connection)
    if path is not None:
        return path
    from backend.database.db import database_path

    return database_path()


if __name__ == "__main__":
    print(initialize_database())
