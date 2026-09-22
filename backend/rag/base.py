from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RagDocument:
    source_url: str
    source_title: str
    content: str


@dataclass(frozen=True, slots=True)
class RagChunk:
    source_url: str
    source_title: str
    index: int
    content: str


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    chunk: RagChunk
    score: float


class DocumentLoader(Protocol):
    async def load(self, url: str) -> RagDocument: ...


class Retriever(Protocol):
    def retrieve(
        self, query: str, chunks: list[RagChunk], limit: int = 5
    ) -> list[RetrievalHit]: ...
