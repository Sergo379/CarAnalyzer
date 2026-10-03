"""Persistent sqlite-vec indices, with SQLite-owned embedding provenance."""

import hashlib
import sqlite3
import struct


def _blob(vector: list[float]) -> bytes:
    if not vector:
        raise ValueError("Empty embedding")
    return struct.pack(f"<{len(vector)}f", *vector)


class SqliteVectorStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        try:
            import sqlite_vec
        except ImportError as exc:
            raise RuntimeError("Install the 'knowledge' extra for sqlite-vec") from exc
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)

    @staticmethod
    def _table(model: str, version: str, dimensions: int) -> str:
        digest = hashlib.sha256(f"{model}\0{version}\0{dimensions}".encode()).hexdigest()[:24]
        return f"knowledge_vec_{digest}"

    def _ensure(self, model: str, version: str, dimensions: int) -> str:
        if not 1 <= dimensions <= 4096:
            raise ValueError("Invalid embedding dimensions")
        table = self._table(model, version, dimensions)
        self.connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(embedding float[{dimensions}])"
        )
        return table

    def add(self, chunk_id: int, model: str, version: str, vector: list[float]) -> bool:
        dimensions = len(vector)
        table = self._ensure(model, version, dimensions)
        existing = self.connection.execute(
            """SELECT 1 FROM knowledge_embeddings WHERE chunk_id=?
             AND embedding_model=? AND embedding_model_version=?""",
            (chunk_id, model, version),
        ).fetchone()
        if existing:
            return False
        payload = _blob(vector)
        self.connection.execute(
            f"INSERT INTO {table}(rowid, embedding) VALUES (?, ?)", (chunk_id, payload)
        )
        self.connection.execute(
            """INSERT INTO knowledge_embeddings
             (chunk_id, embedding_model, embedding_model_version, dimensions, vector)
             VALUES (?, ?, ?, ?, ?)""",
            (chunk_id, model, version, dimensions, payload),
        )
        self.connection.commit()
        return True

    def search(
        self, model: str, version: str, query_vector: list[float], limit: int = 30
    ) -> list[tuple[int, float]]:
        if limit <= 0:
            return []
        table = self._table(model, version, len(query_vector))
        if not self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
        ).fetchone():
            return []
        rows = self.connection.execute(
            f"""SELECT rowid, distance FROM {table}
             WHERE embedding MATCH ? ORDER BY distance LIMIT ?""",
            (_blob(query_vector), limit),
        ).fetchall()
        return [(int(row[0]), float(row[1])) for row in rows]
