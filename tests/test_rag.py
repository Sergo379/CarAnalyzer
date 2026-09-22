from backend.rag.base import RagDocument
from backend.rag.chunker import TextChunker
from backend.rag.retriever import LexicalRetriever


def test_chunker_preserves_source_and_overlap() -> None:
    document = RagDocument(
        source_url="https://example.com/bmw",
        source_title="BMW inspection",
        content=" ".join(f"word{index}" for index in range(55)),
    )
    chunks = TextChunker(chunk_words=20, overlap_words=5).chunk(document)
    assert len(chunks) == 4
    assert chunks[0].source_url == document.source_url
    assert chunks[0].content.split()[-5:] == chunks[1].content.split()[:5]


def test_retriever_returns_relevant_grounded_chunks() -> None:
    chunker = TextChunker(chunk_words=20, overlap_words=0)
    documents = [
        RagDocument("https://example.com/engine", "Engine", "цепь грм шум двигатель ремонт"),
        RagDocument("https://example.com/paint", "Paint", "краска кузов сколы лак"),
    ]
    chunks = [chunk for document in documents for chunk in chunker.chunk(document)]
    hits = LexicalRetriever().retrieve("проверить двигатель и цепь грм", chunks, limit=2)
    assert hits
    assert hits[0].chunk.source_title == "Engine"
    assert hits[0].score > 0
