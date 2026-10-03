"""Build or explicitly rebuild one canonical technical profile (bounded sources)."""

import argparse
import asyncio
import json
from dataclasses import asdict

import httpx

from backend.knowledge.embeddings import LocalSentenceEmbedding
from backend.knowledge.grabber import TechnicalGrabber
from backend.knowledge.identity import resolve_knowledge_identity
from backend.knowledge.retrieval import LocalCrossEncoderReranker
from backend.knowledge.service import TechnicalKnowledgeService
from backend.services.marketplace_catalog import get_catalog_cache


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--generation-id")
    parser.add_argument("--engine-id")
    parser.add_argument("--modification-id")
    parser.add_argument("--rebuild", action="store_true", help="Regenerate the stored profile")
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Probe bounded public discovery without database writes or Gemini",
    )
    parser.add_argument("--refresh-sources", action="store_true", help="Refetch bounded sources")
    parser.add_argument("--max-sources", type=int, default=6)
    parser.add_argument(
        "--embedding-model", choices=["e5-small", "minilm-multilingual"], default="e5-small"
    )
    parser.add_argument("--reranker", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_sources <= 20:
        parser.error("--max-sources must be between 1 and 20")
    if args.refresh_sources and not args.rebuild:
        parser.error("--refresh-sources requires --rebuild")
    identity = resolve_knowledge_identity(
        get_catalog_cache(),
        args.brand,
        args.model,
        args.generation_id,
        args.engine_id,
        args.modification_id,
    )
    if args.discover_only:
        grabber = TechnicalGrabber(max_sources=args.max_sources)
        async with httpx.AsyncClient(
            timeout=grabber.timeout_seconds,
            headers={"User-Agent": "CarAnalyzer-Knowledge/0.1"},
        ) as client:
            discovery = await grabber.discover_with_report(identity, client)
            candidates = discovery.candidates
            loaded = [await grabber.load(item, client) for item in candidates]
        print(
            json.dumps(
                {
                    "sources_discovered": len(candidates),
                    "queries_generated": len(
                        {attempt.query for attempt in discovery.attempts if attempt.query}
                    ),
                    "discovery_attempts": [asdict(attempt) for attempt in discovery.attempts],
                    "sources": [
                        {
                            "url": item.candidate.url,
                            "status": item.status,
                            "content_chars": len(item.document.content) if item.document else 0,
                        }
                        for item in loaded
                    ],
                },
                ensure_ascii=False,
            )
        )
        return
    service = TechnicalKnowledgeService(
        grabber=TechnicalGrabber(max_sources=args.max_sources),
        embedder=LocalSentenceEmbedding(args.embedding_model),
        reranker=LocalCrossEncoderReranker() if args.reranker else None,
    )
    result = await service.build(identity, force=args.rebuild, refresh_sources=args.refresh_sources)
    print(
        json.dumps(
            {
                "status": result.status,
                "scope_key": result.scope_key,
                "diagnostics": asdict(result.diagnostics),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
