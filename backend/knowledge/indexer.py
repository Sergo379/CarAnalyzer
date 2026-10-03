"""Batch index only chunks missing an embedding for the selected model version."""

import sqlite3

from backend.knowledge.embeddings import EmbeddingProvider
from backend.knowledge.vector_store import SqliteVectorStore


class KnowledgeIndexer:
    def __init__(
        self,
        connection: sqlite3.Connection,
        provider: EmbeddingProvider,
        vector_store: SqliteVectorStore,
        batch_size: int = 16,
    ) -> None:
        self.connection = connection
        self.provider = provider
        self.vector_store = vector_store
        self.batch_size = batch_size

    async def index_document(self, document_id: int) -> int:
        rows = self.connection.execute(
            """SELECT c.id, c.content FROM knowledge_chunks c
             LEFT JOIN knowledge_embeddings e ON e.chunk_id=c.id
               AND e.embedding_model=? AND e.embedding_model_version=?
             WHERE c.document_id=? AND e.chunk_id IS NULL ORDER BY c.chunk_index""",
            (self.provider.model_name, self.provider.model_version, document_id),
        ).fetchall()
        created = 0
        for start in range(0, len(rows), self.batch_size):
            batch = rows[start : start + self.batch_size]
            vectors = await self.provider.embed([str(row[1]) for row in batch])
            if len(vectors) != len(batch):
                raise ValueError("Embedding provider returned the wrong vector count")
            for row, vector in zip(batch, vectors, strict=True):
                if self.vector_store.add(
                    int(row[0]), self.provider.model_name, self.provider.model_version, vector
                ):
                    created += 1
        return created
