"""Reproducible PAD Lab 1 retrieval experiments over a synthetic corpus."""

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from backend.database.init_db import initialize_connection
from backend.knowledge.embeddings import EmbeddingProvider
from backend.knowledge.evidence_policy import select_usable_evidence
from backend.knowledge.gemini import GeminiProvider
from backend.knowledge.identity import KnowledgeIdentity
from backend.knowledge.indexer import KnowledgeIndexer
from backend.knowledge.llama_ingest import LlamaIndexIngestAdapter
from backend.knowledge.processing import CleanDocument, chunk_document
from backend.knowledge.profile import validate_grounded_answer
from backend.knowledge.repository import KnowledgeRepository
from backend.knowledge.retrieval import HybridRetriever, Reranker
from backend.knowledge.vector_store import SqliteVectorStore
from lab1.evaluation.metrics import retrieval_metrics

DIRECTORY = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    chunk_strategy: str = "section"
    chunk_words: int = 100
    overlap_words: int = 15
    top_k: int = 5
    lexical_weight: float = 0.5
    similarity_threshold: float = -1e9
    reranker: bool = False
    prompt_version: str = "grounded-v1"


def load_benchmark() -> tuple[dict, list[dict]]:
    corpus = json.loads((DIRECTORY / "corpus.json").read_text(encoding="utf-8"))
    questions = json.loads((DIRECTORY / "questions.json").read_text(encoding="utf-8"))
    if len(questions) < 30:
        raise ValueError("PAD Lab 1 requires at least 30 evaluation questions")
    if len({item["id"] for item in questions}) != len(questions):
        raise ValueError("Evaluation question IDs must be unique")
    return corpus, questions


def _scope(vehicle: dict, level: str) -> KnowledgeIdentity:
    return KnowledgeIdentity(
        vehicle["brand_id"],
        vehicle["model_id"],
        vehicle["generation_id"] if level in {"generation", "engine"} else None,
        vehicle["engine_id"] if level == "engine" else None,
        brand=vehicle["brand"],
        model=vehicle["model"],
        generation=vehicle["generation"] if level in {"generation", "engine"} else "",
        engine=vehicle["engine"] if level == "engine" else "",
    )


async def evaluate(
    config: ExperimentConfig,
    embedder: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
    generator: GeminiProvider | None = None,
) -> dict:
    corpus, questions = load_benchmark()
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        initialize_connection(connection)
        repo = KnowledgeRepository(connection)
        document_slugs: dict[int, str] = {}
        for item in corpus["documents"]:
            content = item["content"]
            document = CleanDocument(
                item["title"], content, "en", sha256(content.encode()).hexdigest()
            )
            identity = _scope(corpus["vehicle"], item["scope"])
            chunks = (
                LlamaIndexIngestAdapter().chunk(document, identity)
                if config.chunk_strategy == "llama-section"
                else chunk_document(
                    document,
                    config.chunk_strategy,
                    config.chunk_words,
                    config.overlap_words,
                )
            )
            saved = repo.save_document(
                identity,
                item["url"],
                "synthetic",
                document,
                chunks,
                corpus_kind="evaluation",
            )
            document_slugs[saved.document_id] = item["id"]
        vectors = SqliteVectorStore(connection) if embedder else None
        if embedder and vectors:
            indexer = KnowledgeIndexer(connection, embedder, vectors)
            for document_id in document_slugs:
                await indexer.index_document(document_id)
        chunk_slugs = {
            int(row[0]): document_slugs[int(row[1])]
            for row in connection.execute("SELECT id, document_id FROM knowledge_chunks")
        }
        identity = _scope(corpus["vehicle"], "engine")
        retriever = HybridRetriever(
            connection,
            vectors,
            reranker if config.reranker else None,
            corpus_kind="evaluation",
        )
        rows: list[dict] = []
        input_tokens = output_tokens = 0
        for question in questions:
            query = question["question"]
            started = perf_counter()
            vector = (await embedder.embed([query], query=True))[0] if embedder else None
            hits, diagnostics = retriever.retrieve(
                query,
                identity,
                query_vector=vector,
                embedding_model=embedder.model_name if embedder else "",
                embedding_version=embedder.model_version if embedder else "default",
                top_k=config.top_k,
                lexical_weight=config.lexical_weight,
            )
            hits = [hit for hit in hits if hit.score >= config.similarity_threshold]
            docs = [chunk_slugs[hit.chunk_id] for hit in hits]
            metrics = retrieval_metrics(docs, question["expected_evidence"], config.top_k)
            row = {
                "id": question["id"],
                "category": question["category"],
                "answerable": question["answerable"],
                "vehicle_scope": identity.scope_key,
                "expected_evidence": question["expected_evidence"],
                "retrieved_documents": docs,
                "retrieved_chunk_ids": [hit.chunk_id for hit in hits],
                "retrieval_scores": [hit.score for hit in hits],
                "metrics": asdict(metrics),
                "context_relevance": (
                    sum(doc in question["expected_evidence"] for doc in docs) / len(docs)
                    if docs and question["answerable"]
                    else 0.0
                ),
                "unanswerable_false_positive": bool(hits) if not question["answerable"] else False,
                "latency_ms": int((perf_counter() - started) * 1000),
                "diagnostics": asdict(diagnostics),
            }
            if config.reranker:
                graded = select_usable_evidence(
                    hits, identity, reranked=True, corpus_kind="evaluation"
                )
                row["legacy_policy_documents"] = [
                    chunk_slugs[hit.chunk_id] for hit in hits if hit.score >= 0
                ]
                row["graded_policy_documents"] = [chunk_slugs[hit.chunk_id] for hit in graded.hits]
                row["graded_policy_tiers"] = graded.tiers
            if generator is not None:
                answer_result = await generator.answer_question(
                    identity, query, hits, config.prompt_version
                )
                answer = validate_grounded_answer(answer_result.draft, hits, identity)
                input_tokens += answer_result.input_tokens
                output_tokens += answer_result.output_tokens
                row["answer"] = answer.answer
                row["answer_evidence_refs"] = answer.evidence_refs
                row["generation_latency_ms"] = answer_result.latency_ms
                row["faithfulness_citation_proxy"] = float(
                    bool(answer.insufficient_evidence) or bool(answer.evidence_refs)
                )
                row["answer_relevance_fact_proxy"] = (
                    sum(
                        fact.casefold() in answer.answer.casefold()
                        for fact in question["expected_facts"]
                    )
                    / len(question["expected_facts"])
                    if question["expected_facts"]
                    else float(not answer.answer)
                )
            rows.append(row)
        answerable = [row for row in rows if row["answerable"]]
        unanswerable = [row for row in rows if not row["answerable"]]
        summary = {
            key: sum(row["metrics"][key] for row in answerable) / len(answerable)
            for key in ("recall_at_k", "precision_at_k", "mrr", "hit_rate")
        }
        summary["context_relevance"] = sum(row["context_relevance"] for row in answerable) / len(
            answerable
        )
        summary["unanswerable_false_positive_rate"] = sum(
            row["unanswerable_false_positive"] for row in unanswerable
        ) / len(unanswerable)
        summary["mean_latency_ms"] = sum(row["latency_ms"] for row in rows) / len(rows)
        if config.reranker:
            answerable_scores = sorted(
                row["retrieval_scores"][0] for row in answerable if row["retrieval_scores"]
            )
            unanswerable_scores = sorted(
                row["retrieval_scores"][0] for row in unanswerable if row["retrieval_scores"]
            )
            summary["min_answerable_top_score"] = min(answerable_scores, default=0.0)
            summary["max_unanswerable_top_score"] = max(unanswerable_scores, default=0.0)
        if generator is not None:
            summary["faithfulness_citation_proxy"] = sum(
                row["faithfulness_citation_proxy"] for row in rows
            ) / len(rows)
            summary["answer_relevance_fact_proxy"] = sum(
                row["answer_relevance_fact_proxy"] for row in rows
            ) / len(rows)
            summary["input_tokens"] = input_tokens
            summary["output_tokens"] = output_tokens
            summary["mean_generation_latency_ms"] = sum(
                row["generation_latency_ms"] for row in rows
            ) / len(rows)
        result = {
            "config": asdict(config),
            "embedding_model": embedder.model_name if embedder else "lexical-only",
            "embedding_version": embedder.model_version if embedder else "none",
            "reranker_model": getattr(reranker, "model_name", "none")
            if config.reranker
            else "none",
            "question_count": len(rows),
            "summary": summary,
            "rows": rows,
            "generation_metrics_status": "deterministic_proxy_run"
            if generator
            else "not_run_without_gemini_key",
        }
        return result
    finally:
        connection.close()


def log_mlflow(result: dict, tracking_directory: Path) -> str:
    try:
        import mlflow
    except ImportError as exc:
        raise RuntimeError("Install the 'lab' extra for MLflow tracking") from exc
    tracking_directory.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(f"sqlite:///{(tracking_directory / 'mlflow.db').as_posix()}")
    mlflow.set_experiment("CarAnalyzer PAD Lab 1")
    with mlflow.start_run() as run:
        mlflow.log_params(
            {
                **result["config"],
                "embedding_model": result["embedding_model"],
                "embedding_version": result["embedding_version"],
                "reranker_model": result["reranker_model"],
                "question_count": result["question_count"],
            }
        )
        mlflow.log_metrics(result["summary"])
        mlflow.log_dict(result, "evaluation.json")
        return run.info.run_id


def compare_evidence_policies(result: dict) -> dict:
    """Compare context availability/safety proxies on identical reranked candidates.

    This is not a generated-claim or human semantic-faithfulness measurement.
    """
    if not result["config"]["reranker"] or result["config"]["similarity_threshold"] > -1:
        raise ValueError("Comparison needs ungated reranked candidates")
    rows = result["rows"]
    answerable = [row for row in rows if row["answerable"]]
    unanswerable = [row for row in rows if not row["answerable"]]

    def metrics(key: str) -> dict[str, float]:
        def supported(row: dict) -> bool:
            return bool(set(row[key]) & set(row["expected_evidence"]))

        selected = [row for row in answerable if row[key]]
        relevant = sum(
            document in row["expected_evidence"] for row in answerable for document in row[key]
        )
        total = sum(len(row[key]) for row in answerable)
        return {
            "answer_coverage_proxy": sum(supported(row) for row in answerable) / len(answerable),
            "useful_supported_information_rate": (
                sum(supported(row) for row in answerable) / len(answerable)
            ),
            "context_relevance": relevant / total if total else 0.0,
            "unsupported_context_fraction": (total - relevant) / total if total else 0.0,
            "unanswerable_context_false_positive_rate": (
                sum(bool(row[key]) for row in unanswerable) / len(unanswerable)
            ),
            "answerable_context_rate": len(selected) / len(answerable),
        }

    return {
        "dataset": "PAD Lab 1 synthetic 40-question corpus",
        "configuration": result["config"],
        "old_binary": metrics("legacy_policy_documents"),
        "graded": metrics("graded_policy_documents"),
        "unsupported_claim_rate": None,
        "semantic_faithfulness": None,
        "measurement_limit": (
            "No Gemini answers or human entailment labels were generated. "
            "Unsupported-context fraction and unanswerable-context FPR are risk proxies, "
            "not unsupported-claim or faithfulness measurements."
        ),
    }


def log_policy_comparison(comparison: dict, tracking_directory: Path) -> str:
    try:
        import mlflow
    except ImportError as exc:
        raise RuntimeError("Install the 'lab' extra for MLflow tracking") from exc
    tracking_directory.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(f"sqlite:///{(tracking_directory / 'mlflow.db').as_posix()}")
    mlflow.set_experiment("CarAnalyzer PAD Lab 1")
    with mlflow.start_run(run_name="binary-vs-graded-evidence") as run:
        mlflow.log_params({"policy_comparison": "binary-vs-graded", **comparison["configuration"]})
        mlflow.log_metrics(
            {
                f"{policy}_{name}": value
                for policy in ("old_binary", "graded")
                for name, value in comparison[policy].items()
            }
        )
        mlflow.log_dict(comparison, "evidence_policy_comparison.json")
        return run.info.run_id


def persist_experiment(result: dict, database_path: Path, mlflow_run_id: str | None) -> int:
    _, questions = load_benchmark()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        initialize_connection(connection)
        scope = result["rows"][0]["vehicle_scope"]
        connection.executemany(
            """INSERT INTO rag_evaluation_questions
             (id, question, scope_key, expected_evidence, expected_facts, answerable, category)
             VALUES (?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT(id) DO NOTHING""",
            [
                (
                    item["id"],
                    item["question"],
                    scope,
                    json.dumps(item["expected_evidence"]),
                    json.dumps(item["expected_facts"]),
                    int(item["answerable"]),
                    item["category"],
                )
                for item in questions
            ],
        )
        cursor = connection.execute(
            """INSERT INTO rag_experiment_runs
             (created_at, configuration, metrics, mlflow_run_id) VALUES (?, ?, ?, ?)""",
            (
                datetime.now(UTC).isoformat(),
                json.dumps(
                    {
                        **result["config"],
                        "embedding_model": result["embedding_model"],
                        "embedding_version": result["embedding_version"],
                        "reranker_model": result["reranker_model"],
                    }
                ),
                json.dumps(result["summary"]),
                mlflow_run_id,
            ),
        )
        run_id = int(cursor.lastrowid)
        connection.executemany(
            """INSERT INTO rag_evaluation_results
             (run_id, question_id, retrieved_chunks, metrics) VALUES (?, ?, ?, ?)""",
            [
                (
                    run_id,
                    row["id"],
                    json.dumps(row["retrieved_chunk_ids"]),
                    json.dumps({**row["metrics"], "context_relevance": row["context_relevance"]}),
                )
                for row in result["rows"]
            ],
        )
        return run_id


def evaluate_sync(
    config: ExperimentConfig,
    embedder: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
    generator: GeminiProvider | None = None,
) -> dict:
    return asyncio.run(evaluate(config, embedder, reranker, generator))
