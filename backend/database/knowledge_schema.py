"""Additive, versioned technical-knowledge storage.

These tables deliberately do not alter the legacy market-search knowledge tables.
"""

import sqlite3

KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_sources (
 id INTEGER PRIMARY KEY,
 scope_key TEXT NOT NULL,
 canonical_brand_id TEXT NOT NULL,
 canonical_model_id TEXT NOT NULL,
 canonical_generation_id TEXT,
 canonical_engine_id TEXT,
 canonical_modification_id TEXT,
 domain TEXT NOT NULL,
 source_type TEXT NOT NULL,
 corpus_kind TEXT NOT NULL DEFAULT 'production',
 discovery_provider TEXT NOT NULL DEFAULT '',
 discovery_query TEXT NOT NULL DEFAULT '',
 url TEXT NOT NULL,
 title TEXT NOT NULL DEFAULT '',
 discovered_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'discovered',
 error_type TEXT,
 UNIQUE(scope_key, url)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_sources_scope ON knowledge_sources(scope_key);
CREATE TABLE IF NOT EXISTS knowledge_discovery_attempts (
 id INTEGER PRIMARY KEY,
 build_id TEXT NOT NULL,
 scope_key TEXT NOT NULL,
 provider TEXT NOT NULL,
 query TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL,
 result_count INTEGER NOT NULL DEFAULT 0,
 error_type TEXT,
 error_message TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_discovery_attempts_scope
 ON knowledge_discovery_attempts(scope_key, created_at);
CREATE TABLE IF NOT EXISTS knowledge_builds (
 build_id TEXT PRIMARY KEY,
 scope_key TEXT NOT NULL,
 status TEXT NOT NULL,
 phase TEXT NOT NULL,
 started_at TEXT NOT NULL,
 finished_at TEXT,
 diagnostics_json TEXT NOT NULL DEFAULT '{}',
 last_error TEXT NOT NULL DEFAULT '',
 error_type TEXT NOT NULL DEFAULT '',
 profile_persisted INTEGER NOT NULL DEFAULT 0 CHECK(profile_persisted IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_knowledge_builds_scope
 ON knowledge_builds(scope_key, started_at DESC);
CREATE TABLE IF NOT EXISTS knowledge_documents (
 id INTEGER PRIMARY KEY,
 scope_key TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 content TEXT NOT NULL,
 language TEXT NOT NULL DEFAULT 'unknown',
 fetched_at TEXT NOT NULL,
 metadata TEXT NOT NULL DEFAULT '{}',
 UNIQUE(scope_key, content_hash)
);
CREATE TABLE IF NOT EXISTS knowledge_source_documents (
 source_id INTEGER PRIMARY KEY REFERENCES knowledge_sources(id) ON DELETE RESTRICT,
 document_id INTEGER NOT NULL REFERENCES knowledge_documents(id) ON DELETE RESTRICT,
 linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
 id INTEGER PRIMARY KEY,
 document_id INTEGER NOT NULL REFERENCES knowledge_documents(id) ON DELETE RESTRICT,
 chunk_index INTEGER NOT NULL,
 content TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 section TEXT NOT NULL DEFAULT '',
 component TEXT NOT NULL DEFAULT '',
 language TEXT NOT NULL DEFAULT 'unknown',
 metadata TEXT NOT NULL DEFAULT '{}',
 UNIQUE(document_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document ON knowledge_chunks(document_id);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
 content, content='knowledge_chunks', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS knowledge_chunks_fts_insert AFTER INSERT ON knowledge_chunks BEGIN
 INSERT INTO knowledge_chunks_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_chunks_fts_delete AFTER DELETE ON knowledge_chunks BEGIN
 INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts, rowid, content)
 VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_chunks_fts_update AFTER UPDATE ON knowledge_chunks BEGIN
 INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts, rowid, content)
 VALUES ('delete', old.id, old.content);
 INSERT INTO knowledge_chunks_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TABLE IF NOT EXISTS knowledge_embeddings (
 chunk_id INTEGER NOT NULL REFERENCES knowledge_chunks(id) ON DELETE RESTRICT,
 embedding_model TEXT NOT NULL,
 embedding_model_version TEXT NOT NULL,
 dimensions INTEGER NOT NULL CHECK(dimensions > 0),
 vector BLOB NOT NULL,
 PRIMARY KEY(chunk_id, embedding_model, embedding_model_version)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_model
 ON knowledge_embeddings(embedding_model, embedding_model_version);
CREATE TABLE IF NOT EXISTS problem_profiles (
 id INTEGER PRIMARY KEY,
 scope_key TEXT NOT NULL UNIQUE,
 canonical_brand_id TEXT NOT NULL,
 canonical_model_id TEXT NOT NULL,
 canonical_generation_id TEXT,
 canonical_engine_id TEXT,
 canonical_modification_id TEXT,
 status TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 pipeline_version TEXT NOT NULL,
 embedding_model TEXT NOT NULL,
 llm_model TEXT NOT NULL,
 source_count INTEGER NOT NULL DEFAULT 0,
 evidence_count INTEGER NOT NULL DEFAULT 0,
 confidence TEXT NOT NULL DEFAULT 'low'
);
CREATE TABLE IF NOT EXISTS problem_claims (
 id INTEGER PRIMARY KEY,
 profile_id INTEGER NOT NULL REFERENCES problem_profiles(id) ON DELETE RESTRICT,
 category TEXT NOT NULL,
 component TEXT NOT NULL DEFAULT '',
 title TEXT NOT NULL,
 description TEXT NOT NULL,
 scope_key TEXT NOT NULL,
 confidence TEXT NOT NULL,
 UNIQUE(profile_id, category, title)
);
CREATE TABLE IF NOT EXISTS claim_evidence (
 claim_id INTEGER NOT NULL REFERENCES problem_claims(id) ON DELETE RESTRICT,
 chunk_id INTEGER NOT NULL REFERENCES knowledge_chunks(id) ON DELETE RESTRICT,
 PRIMARY KEY(claim_id, chunk_id)
);
CREATE TABLE IF NOT EXISTS rag_evaluation_questions (
 id TEXT PRIMARY KEY,
 question TEXT NOT NULL,
 scope_key TEXT NOT NULL,
 expected_evidence TEXT NOT NULL DEFAULT '[]',
 expected_facts TEXT NOT NULL DEFAULT '[]',
 answerable INTEGER NOT NULL CHECK(answerable IN (0, 1)),
 category TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_experiment_runs (
 id INTEGER PRIMARY KEY,
 created_at TEXT NOT NULL,
 configuration TEXT NOT NULL,
 metrics TEXT NOT NULL DEFAULT '{}',
 mlflow_run_id TEXT
);
CREATE TABLE IF NOT EXISTS rag_evaluation_results (
 run_id INTEGER NOT NULL REFERENCES rag_experiment_runs(id) ON DELETE RESTRICT,
 question_id TEXT NOT NULL REFERENCES rag_evaluation_questions(id) ON DELETE RESTRICT,
 retrieved_chunks TEXT NOT NULL DEFAULT '[]',
 answer TEXT,
 metrics TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(run_id, question_id)
);
"""


def initialize_knowledge_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(KNOWLEDGE_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(knowledge_sources)")}
    migrations = {
        "corpus_kind": "TEXT NOT NULL DEFAULT 'production'",
        "discovery_provider": "TEXT NOT NULL DEFAULT ''",
        "discovery_query": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in migrations.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE knowledge_sources ADD COLUMN {name} {definition}")
    attempt_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(knowledge_discovery_attempts)")
    }
    if "language" not in attempt_columns:
        connection.execute(
            "ALTER TABLE knowledge_discovery_attempts ADD COLUMN language TEXT NOT NULL DEFAULT ''"
        )
    if "error_message" not in attempt_columns:
        connection.execute(
            """ALTER TABLE knowledge_discovery_attempts
               ADD COLUMN error_message TEXT NOT NULL DEFAULT ''"""
        )
    connection.execute(
        """DELETE FROM knowledge_discovery_attempts
           WHERE id NOT IN (
             SELECT min(id) FROM knowledge_discovery_attempts
             GROUP BY build_id, provider, query, language
           )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_discovery_attempt_unique
           ON knowledge_discovery_attempts(build_id, provider, query, language)"""
    )
    connection.execute(
        """UPDATE knowledge_sources SET corpus_kind='evaluation'
           WHERE source_type IN ('synthetic', 'evaluation', 'fixture', 'test')"""
    )
