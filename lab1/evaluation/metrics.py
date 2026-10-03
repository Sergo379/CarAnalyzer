"""Retrieval and grounded-generation metrics with explicit limitations."""

from dataclasses import dataclass

from backend.knowledge.profile import ProfileDraft
from backend.knowledge.retrieval import KnowledgeHit


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    recall_at_k: float
    precision_at_k: float
    mrr: float
    hit_rate: float


def retrieval_metrics(
    retrieved_documents: list[str], expected_documents: list[str], k: int
) -> RetrievalMetrics:
    if k <= 0:
        raise ValueError("k must be positive")
    if not expected_documents:
        return RetrievalMetrics(0.0, 0.0, 0.0, 0.0)
    expected = set(expected_documents)
    ranked = retrieved_documents[:k]
    relevant = [index for index, identifier in enumerate(ranked, 1) if identifier in expected]
    return RetrievalMetrics(
        recall_at_k=len({item for item in ranked if item in expected}) / len(expected),
        precision_at_k=len(relevant) / k,
        mrr=1 / relevant[0] if relevant else 0.0,
        hit_rate=float(bool(relevant)),
    )


def deterministic_generation_metrics(
    draft: ProfileDraft,
    evidence: list[KnowledgeHit],
    expected_facts: list[str],
) -> dict[str, float]:
    """Proxy metrics, not an LLM judge or a semantic truth guarantee."""
    valid_refs = {hit.chunk_id for hit in evidence}
    claims = [
        claim
        for category in (
            draft.common_problems,
            draft.problematic_components,
            draft.inspection_points,
            draft.expensive_failures,
        )
        for claim in category
    ]
    cited = sum(
        bool(claim.evidence_refs) and set(claim.evidence_refs) <= valid_refs for claim in claims
    )
    answer = " ".join(
        [draft.risk_summary, *(claim.title + " " + claim.description for claim in claims)]
    ).casefold()
    return {
        "faithfulness_citation_proxy": cited / len(claims) if claims else 1.0,
        "answer_relevance_fact_proxy": (
            sum(fact.casefold() in answer for fact in expected_facts) / len(expected_facts)
            if expected_facts
            else 1.0
        ),
    }
