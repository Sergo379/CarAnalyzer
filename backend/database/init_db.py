import sqlite3
from pathlib import Path

from backend.database.db import connect
from backend.database.knowledge_schema import initialize_knowledge_schema
from backend.services.segment_classifier import SEGMENT_SCHEMA

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
CREATE TABLE IF NOT EXISTS car_profiles (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 brand TEXT NOT NULL,
 model TEXT NOT NULL,
 year INTEGER NOT NULL,
 generation TEXT NOT NULL DEFAULT '',
 segment TEXT,
 supported_body_types TEXT NOT NULL DEFAULT '[]',
 technical_summary TEXT,
 knowledge_updated_at TEXT,
 UNIQUE (brand, model, year, generation)
);
CREATE TABLE IF NOT EXISTS car_problem_profiles (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 car_profile_id INTEGER NOT NULL UNIQUE REFERENCES car_profiles(id) ON DELETE RESTRICT,
 common_problems TEXT NOT NULL DEFAULT '[]',
 problematic_components TEXT NOT NULL DEFAULT '[]',
 inspection_points TEXT NOT NULL DEFAULT '[]',
 expensive_failures TEXT NOT NULL DEFAULT '[]',
 risk_summary TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_documents (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 car_profile_id INTEGER NOT NULL REFERENCES car_profiles(id) ON DELETE RESTRICT,
 source_url TEXT NOT NULL,
 source_title TEXT NOT NULL,
 source_type TEXT NOT NULL DEFAULT 'web',
 fetched_at TEXT NOT NULL,
 content_hash TEXT NOT NULL DEFAULT '',
 content TEXT NOT NULL,
 metadata TEXT NOT NULL DEFAULT '{}',
 UNIQUE (car_profile_id, source_url)
);
CREATE TABLE IF NOT EXISTS rag_chunks (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 document_id INTEGER NOT NULL REFERENCES rag_documents(id) ON DELETE RESTRICT,
 chunk_index INTEGER NOT NULL,
 content TEXT NOT NULL,
 embedding TEXT,
 UNIQUE (document_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_car_profiles_lookup
ON car_profiles(brand, model, year);
CREATE INDEX IF NOT EXISTS idx_rag_documents_profile
ON rag_documents(car_profile_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_document
ON rag_chunks(document_id);
"""


def initialize_connection(connection: sqlite3.Connection) -> None:
    """Apply the non-destructive schema to an open SQLite connection."""
    connection.executescript(SCHEMA)
    migrations = {
        "rag_documents": {
            "source_type": "TEXT NOT NULL DEFAULT 'web'",
            "content_hash": "TEXT NOT NULL DEFAULT ''",
        },
        "rag_chunks": {"metadata": "TEXT NOT NULL DEFAULT '{}'"},
    }
    for table, columns in migrations.items():
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    initialize_knowledge_schema(connection)
    connection.executescript(SEGMENT_SCHEMA)
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
