"""Run bounded synthetic PAD Lab 1 retrieval experiments; no live car crawling."""

import argparse
import asyncio
import json
from dataclasses import asdict

from backend.config import PROJECT_ROOT
from backend.knowledge.embeddings import LocalSentenceEmbedding
from backend.knowledge.gemini import GeminiProvider
from backend.knowledge.retrieval import LocalCrossEncoderReranker
from lab1.evaluation.runner import (
    ExperimentConfig,
    evaluate,
    log_mlflow,
    persist_experiment,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite", action="store_true", help="Compare chunking and two embedding models"
    )
    parser.add_argument(
        "--tune", action="store_true", help="Compare Top-K, hybrid weights, filtering and reranking"
    )
    parser.add_argument(
        "--embedding-model", choices=["none", "e5-small", "minilm-multilingual"], default="none"
    )
    parser.add_argument(
        "--chunk-strategy",
        choices=["fixed", "overlap", "paragraph", "section", "llama-section"],
        default="section",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--lexical-weight", type=float, default=0.5)
    parser.add_argument("--similarity-threshold", type=float, default=-1e9)
    parser.add_argument("--reranker", action="store_true")
    parser.add_argument(
        "--generation", action="store_true", help="Run optional Gemini answer evaluation"
    )
    parser.add_argument(
        "--prompt-version",
        choices=["grounded-v1", "grounded-conservative-v2"],
        default="grounded-v1",
    )
    parser.add_argument("--no-mlflow", action="store_true")
    args = parser.parse_args()

    output = PROJECT_ROOT / "lab1" / "results" / "experiments.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    results = json.loads(output.read_text(encoding="utf-8")) if output.exists() else []
    if args.suite and args.tune:
        parser.error("Choose either --suite or --tune")
    if args.generation and (args.suite or args.tune):
        parser.error("Run generation evaluation one prompt variant at a time")
    generator = GeminiProvider() if args.generation else None
    if generator is not None and not generator.configured:
        parser.error(
            "Gemini is not configured; set GEMINI_API_KEY locally before generation evaluation"
        )
    if args.suite:
        configs = [
            (model, ExperimentConfig(chunk_strategy=strategy, chunk_words=40, overlap_words=8))
            for model in ("e5-small", "minilm-multilingual")
            for strategy in ("fixed", "overlap", "paragraph", "section")
        ]
    elif args.tune:
        base = ExperimentConfig(chunk_strategy="overlap", chunk_words=40, overlap_words=8)
        configs = (
            [
                ("e5-small", ExperimentConfig(**{**asdict(base), "top_k": top_k}))
                for top_k in (3, 5, 10, 20)
            ]
            + [
                ("e5-small", ExperimentConfig(**{**asdict(base), "lexical_weight": weight}))
                for weight in (0.2, 0.8)
            ]
            + [
                (
                    "e5-small",
                    ExperimentConfig(**{**asdict(base), "similarity_threshold": threshold}),
                )
                for threshold in (0.007, 0.01)
            ]
            + [("e5-small", ExperimentConfig(**{**asdict(base), "reranker": True}))]
        )
    else:
        configs = [
            (
                args.embedding_model,
                ExperimentConfig(
                    chunk_strategy=args.chunk_strategy,
                    chunk_words=40,
                    overlap_words=8,
                    top_k=args.top_k,
                    lexical_weight=args.lexical_weight,
                    similarity_threshold=args.similarity_threshold,
                    reranker=args.reranker,
                    prompt_version=args.prompt_version,
                ),
            )
        ]
    embedders = {}
    for model_name, config in configs:
        if model_name != "none" and model_name not in embedders:
            embedders[model_name] = LocalSentenceEmbedding(model_name)
        embedder = embedders.get(model_name)
        reranker = LocalCrossEncoderReranker() if config.reranker else None
        result = await evaluate(config, embedder, reranker, generator)
        mlflow_id = None if args.no_mlflow else log_mlflow(result, PROJECT_ROOT / "data" / "mlruns")
        run_id = persist_experiment(result, PROJECT_ROOT / "data" / "rag_experiments.db", mlflow_id)
        compact = {key: value for key, value in result.items() if key != "rows"}
        compact["database_run_id"] = run_id
        compact["mlflow_run_id"] = mlflow_id
        results.append(compact)
        output.write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "model": model_name,
                    "strategy": config.chunk_strategy,
                    "summary": result["summary"],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
