from typing import Protocol


class EmbeddingProvider(Protocol):
    """Replaceable embedding boundary; no live provider is configured yet."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
