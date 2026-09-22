import json
import sqlite3

from backend.rag.base import RagChunk, RagDocument


class RagRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(self, car_profile_id: int, document: RagDocument, chunks: list[RagChunk]) -> int:
        self.connection.execute(
            """
            INSERT INTO rag_documents (
                car_profile_id, source_url, source_title, source_type,
                fetched_at, content_hash, content
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(car_profile_id, source_url) DO UPDATE SET
                source_title=excluded.source_title,
                source_type=excluded.source_type,
                fetched_at=excluded.fetched_at,
                content_hash=excluded.content_hash,
                content=excluded.content
            """,
            (
                car_profile_id,
                document.source_url,
                document.source_title,
                document.source_type,
                document.fetched_at.isoformat(),
                document.content_hash,
                document.content,
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM rag_documents WHERE car_profile_id=? AND source_url=?",
            (car_profile_id, document.source_url),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to persist RAG document")
        document_id = int(row[0])
        self.connection.execute("DELETE FROM rag_chunks WHERE document_id=?", (document_id,))
        self.connection.executemany(
            "INSERT INTO rag_chunks "
            "(document_id, chunk_index, content, metadata) VALUES (?, ?, ?, ?)",
            [
                (document_id, chunk.index, chunk.content, json.dumps(chunk.metadata or {}))
                for chunk in chunks
            ],
        )
        self.connection.commit()
        return document_id
