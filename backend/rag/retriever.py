import math
import re
from collections import Counter

from backend.rag.base import RagChunk, RetrievalHit

_TOKEN = re.compile(r"[a-zа-яё0-9]{2,}", re.IGNORECASE)


class LexicalRetriever:
    """Small local retriever used until an embedding provider is configured."""

    def retrieve(
        self,
        query: str,
        chunks: list[RagChunk],
        limit: int = 5,
    ) -> list[RetrievalHit]:
        if limit <= 0:
            return []
        query_terms = Counter(self._tokens(query))
        if not query_terms:
            return []
        document_frequency = Counter(
            term for chunk in chunks for term in set(self._tokens(chunk.content))
        )
        total = max(len(chunks), 1)
        hits: list[RetrievalHit] = []
        for chunk in chunks:
            terms = Counter(self._tokens(chunk.content))
            score = 0.0
            for term, query_count in query_terms.items():
                if term not in terms:
                    continue
                inverse_frequency = math.log((total + 1) / (document_frequency[term] + 1)) + 1
                score += min(terms[term], 3) * query_count * inverse_frequency
            if score > 0:
                hits.append(RetrievalHit(chunk=chunk, score=round(score, 6)))
        return sorted(hits, key=lambda hit: (-hit.score, hit.chunk.index))[:limit]

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [match.group().casefold() for match in _TOKEN.finditer(text)]
