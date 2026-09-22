from collections.abc import Iterable

from backend.rag.base import RagChunk, RagDocument, RetrievalHit, Retriever
from backend.rag.chunker import TextChunker
from backend.rag.retriever import LexicalRetriever


class RagService:
    def __init__(
        self,
        chunker: TextChunker | None = None,
        retriever: Retriever | None = None,
    ) -> None:
        self.chunker = chunker or TextChunker()
        self.retriever = retriever or LexicalRetriever()

    def prepare(self, documents: Iterable[RagDocument]) -> list[RagChunk]:
        return [chunk for document in documents for chunk in self.chunker.chunk(document)]

    def context(self, query: str, chunks: list[RagChunk], limit: int = 5) -> list[RetrievalHit]:
        return self.retriever.retrieve(query, chunks, limit)
