"""Narrow LlamaIndex ingestion adapter; the rest of CarAnalyzer uses its own types."""

from hashlib import sha256

from backend.knowledge.identity import KnowledgeIdentity
from backend.knowledge.processing import (
    CleanDocument,
    KnowledgeChunk,
    _component,
    _sections,
    chunk_document,
)


class LlamaIndexIngestAdapter:
    def __init__(
        self,
        chunk_tokens: int = 128,
        overlap_tokens: int = 16,
        final_chunk_words: int = 40,
        final_overlap_words: int = 8,
    ) -> None:
        if chunk_tokens < 64 or not 0 <= overlap_tokens < chunk_tokens:
            raise ValueError("Invalid LlamaIndex chunk configuration")
        self.chunk_tokens = chunk_tokens
        self.overlap_tokens = overlap_tokens
        self.final_chunk_words = final_chunk_words
        self.final_overlap_words = final_overlap_words

    def chunk(self, document: CleanDocument, identity: KnowledgeIdentity) -> list[KnowledgeChunk]:
        try:
            from llama_index.core import Document
            from llama_index.core.node_parser import SentenceSplitter
        except ImportError as exc:
            raise RuntimeError("Install the 'knowledge' extra for LlamaIndex ingestion") from exc

        docs = [
            Document(
                text=content,
                metadata={"section": section},
                id_=f"{document.content_hash}:{index}",
            )
            for index, (section, content) in enumerate(_sections(document.content))
        ]
        section_by_document = {doc.id_: str(doc.metadata["section"]) for doc in docs}
        parser = SentenceSplitter(
            chunk_size=self.chunk_tokens,
            chunk_overlap=self.overlap_tokens,
            include_metadata=False,
        )
        nodes = parser.get_nodes_from_documents(docs)
        result: list[KnowledgeChunk] = []
        seen: set[str] = set()
        for node in nodes:
            section = section_by_document.get(node.ref_doc_id or "", "")
            segmented = chunk_document(
                CleanDocument(document.title, node.text, document.language, document.content_hash),
                strategy="overlap",
                chunk_words=self.final_chunk_words,
                overlap_words=self.final_overlap_words,
            )
            for item in segmented:
                content = item.content
                digest = sha256(content.casefold().encode("utf-8")).hexdigest()
                if not content or digest in seen:
                    continue
                seen.add(digest)
                result.append(
                    KnowledgeChunk(len(result), content, section, _component(section), digest)
                )
        return result
