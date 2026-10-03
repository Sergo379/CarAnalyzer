"""Lazy local multilingual embeddings with explicit model identity."""

import asyncio
from pathlib import Path
from typing import Protocol

from backend.config import PROJECT_ROOT

EMBEDDING_MODELS = {
    "e5-small": "intfloat/multilingual-e5-small",
    "minilm-multilingual": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
}


class EmbeddingProvider(Protocol):
    model_name: str
    model_version: str

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


class LocalSentenceEmbedding:
    def __init__(
        self,
        model: str = "e5-small",
        revision: str = "default",
        cache_directory: Path | None = None,
    ) -> None:
        if model not in EMBEDDING_MODELS:
            raise ValueError(f"Unknown embedding model: {model}")
        self.model_name = EMBEDDING_MODELS[model]
        self.model_version = revision
        self.cache_directory = cache_directory or PROJECT_ROOT / "data" / "model_cache"
        self._model = None

    def _embed_sync(self, texts: list[str], query: bool) -> list[list[float]]:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("Install the 'knowledge' extra for local embeddings") from exc
        if self._model is None:
            self._model = SentenceTransformer(
                self.model_name,
                revision=None if self.model_version == "default" else self.model_version,
                cache_folder=str(self.cache_directory),
            )
        values = [
            ("query: " if query else "passage: ") + text
            if "multilingual-e5" in self.model_name
            else text
            for text in texts
        ]
        vectors = self._model.encode(
            values,
            batch_size=16,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.tolist()

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, texts, query)
