from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RagDocument:
    source_url: str
    source_title: str
    content: str
    source_type: str = "web"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            object.__setattr__(self, "content_hash", sha256(self.content.encode()).hexdigest())


@dataclass(frozen=True, slots=True)
class RagChunk:
    source_url: str
    source_title: str
    index: int
    content: str
    metadata: dict[str, object] | None = None


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
