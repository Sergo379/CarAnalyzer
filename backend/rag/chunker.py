import re

from backend.rag.base import RagChunk, RagDocument


class TextChunker:
    def __init__(self, chunk_words: int = 220, overlap_words: int = 35) -> None:
        if chunk_words < 20:
            raise ValueError("chunk_words must be at least 20")
        if overlap_words < 0 or overlap_words >= chunk_words:
            raise ValueError("overlap_words must be between 0 and chunk_words")
        self.chunk_words = chunk_words
        self.overlap_words = overlap_words

    def chunk(self, document: RagDocument) -> list[RagChunk]:
        cleaned = self.clean(document.content)
        words = cleaned.split()
        if not words:
            return []
        step = self.chunk_words - self.overlap_words
        chunks: list[RagChunk] = []
        for index, start in enumerate(range(0, len(words), step)):
            content = " ".join(words[start : start + self.chunk_words])
            if not content:
                break
            chunks.append(
                RagChunk(
                    source_url=document.source_url,
                    source_title=document.source_title,
                    index=index,
                    content=content,
                )
            )
            if start + self.chunk_words >= len(words):
                break
        return chunks

    @staticmethod
    def clean(text: str) -> str:
        without_controls = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
        return re.sub(r"\s+", " ", without_controls).strip()
