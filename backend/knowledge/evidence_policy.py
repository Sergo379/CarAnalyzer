"""Production evidence safety gate and interpretable, non-LLM confidence tiers.

The cross-encoder ranks relevance, not truth. Its score is used as one signal;
provenance, scope, source independence and category determine claim strength.
"""

import math
import re
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import urlsplit

from backend.knowledge.identity import KnowledgeIdentity
from backend.knowledge.retrieval import KnowledgeHit

EvidenceTier = Literal["high", "medium", "limited"]

_TECHNICAL = re.compile(
    r"(?i)\b(?:engine|motor|turbo|transmission|gearbox|brake|suspension|"
    r"fault|failure|problem|repair|recall|service|inspect|check|leak|"
    r"двигател\w*|мотор\w*|коробк\w*|турбин\w*|тормоз\w*|подвеск\w*|"
    r"неисправ\w*|проблем\w*|ремонт\w*|проверк\w*|отзыв\w*|теч\w*)\b"
)
_OWNER_TYPES = {"owner_forum", "forum", "community", "owner_report"}
_CONTEXT_TYPES = {"encyclopedia", "unknown"}
_AUTHORITATIVE_TYPES = {"official", "recall", "service_bulletin"}
_TECHNICAL_TYPES = {"repair", "technical_article", "professional_technical"}


def source_domains(hits: list[KnowledgeHit]) -> set[str]:
    return {
        host.removeprefix("www.")
        for hit in hits
        for url in hit.source_urls
        if (host := urlsplit(url).hostname)
    }


def scope_label(scope_key: str, identity: KnowledgeIdentity) -> str:
    for candidate in identity.fallback():
        if candidate.scope_key == scope_key:
            if candidate.canonical_modification_id:
                return "modification"
            if candidate.canonical_engine_id:
                return "engine"
            if candidate.canonical_generation_id:
                return "generation"
            return "model"
    return "incompatible"


def _valid_provenance(hit: KnowledgeHit, corpus_kind: str) -> bool:
    return bool(
        hit.corpus_kind == corpus_kind
        and hit.document_id > 0
        and hit.source_ids
        and len(hit.source_ids) == len(hit.source_urls) == len(hit.source_types)
        and all(source_id > 0 for source_id in hit.source_ids)
        and all(
            urlsplit(url).scheme in {"http", "https"} and urlsplit(url).hostname
            for url in hit.source_urls
        )
    )


def usable_hit(
    hit: KnowledgeHit,
    identity: KnowledgeIdentity,
    *,
    reranked: bool,
    minimum_relevance_score: float = -1.0,
    corpus_kind: str = "production",
) -> bool:
    """Hard rejection only: incompatible/invalid or clearly nontechnical evidence.

    The permissive reranker floor catches extreme semantic mismatch; it is NOT
    a confidence threshold. A separate technical-content check rejects noise.
    """
    if hit.scope_key not in {scope.scope_key for scope in identity.fallback()}:
        return False
    if not _valid_provenance(hit, corpus_kind) or not hit.content_hash or not hit.content.strip():
        return False
    if not math.isfinite(hit.score):
        return False
    if reranked and hit.score < minimum_relevance_score:
        return False
    return bool(_TECHNICAL.search(" ".join((hit.section, hit.component, hit.content))))


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    hits: list[KnowledgeHit]
    hard_rejected: int
    tiers: dict[str, int]


def grade_evidence(hits: list[KnowledgeHit], identity: KnowledgeIdentity) -> EvidenceTier:
    """Grade cited support, counting domains/documents rather than raw chunks.

    Owner-only and encyclopedia-only support stays LIMITED. Exact strong
    technical/official support is MEDIUM; HIGH additionally needs either
    independent corroboration or a strong authoritative source and relevance.
    Model-wide single-source evidence stays LIMITED regardless of score.
    """
    if not hits:
        return "limited"
    domains = source_domains(hits)
    documents = {hit.document_id for hit in hits}
    types = {item for hit in hits for item in hit.source_types}
    exact = any(hit.scope_key == identity.scope_key for hit in hits)
    specific = any(scope_label(hit.scope_key, identity) != "model" for hit in hits)
    strong_relevance = any(hit.score >= 0 for hit in hits)
    authoritative = bool(types & _AUTHORITATIVE_TYPES)
    technical = bool(types & _TECHNICAL_TYPES)
    professional = bool(types & {"repair", "professional_technical"})
    if types and types <= (_OWNER_TYPES | _CONTEXT_TYPES):
        return "limited"
    if all(scope_label(hit.scope_key, identity) == "model" for hit in hits) and len(domains) < 2:
        return "limited"
    if exact and strong_relevance and (
        (len(domains) >= 2 and len(documents) >= 2 and (technical or authoritative))
        or authoritative
    ):
        return "high"
    if authoritative or professional:
        return "medium"
    if technical and (strong_relevance or len(domains) >= 2):
        return "medium"
    if specific and len(domains) >= 2:
        return "medium"
    return "limited"


def select_usable_evidence(
    hits: list[KnowledgeHit], identity: KnowledgeIdentity, *, reranked: bool,
    minimum_relevance_score: float = -1.0,
    corpus_kind: str = "production",
) -> EvidenceSelection:
    valid: list[KnowledgeHit] = []
    seen: set[str] = set()
    rejected = 0
    tiers = {"high": 0, "medium": 0, "limited": 0}
    for hit in hits:
        if hit.content_hash in seen or not usable_hit(
            hit, identity, reranked=reranked,
            minimum_relevance_score=minimum_relevance_score,
            corpus_kind=corpus_kind,
        ):
            rejected += 1
            continue
        seen.add(hit.content_hash)
        tier = grade_evidence([hit], identity)
        valid.append(replace(hit, evidence_tier=tier))
        tiers[tier] += 1
    return EvidenceSelection(valid, rejected, tiers)
