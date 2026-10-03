"""Strict evidence-backed technical profile and deterministic confidence."""

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.knowledge.evidence_policy import (
    grade_evidence,
    scope_label,
    source_domains,
    usable_hit,
)
from backend.knowledge.identity import KnowledgeIdentity
from backend.knowledge.retrieval import KnowledgeHit

ClaimCategory = Literal[
    "common_problems", "problematic_components", "inspection_points", "expensive_failures"
]
Confidence = Literal["limited", "medium", "high"]
InspectionMethod = Literal[
    "visual",
    "sound",
    "feel",
    "test_drive",
    "cold_start",
    "controls",
    "documents",
    "under_hood",
    "service_required",
]
InspectionCategory = Literal[
    "body",
    "suspension",
    "steering",
    "engine",
    "cooling",
    "transmission",
    "drivetrain",
    "brakes",
    "electronics",
    "interior",
    "service_history",
    "other",
]
_TECHNICAL_CODE = re.compile(r"\b(?:[A-Z]{1,4}\d{2,5}[A-Z0-9]*|P\d{4})\b")
_REPAIR_EVIDENCE = re.compile(
    r"(?i)\b(?:repair\w*|replac\w*|rebuild\w*|overhaul\w*|cost\w*|"
    r"expensive|costly|ремонт\w*|замен\w*|стоимост\w*|дорог\w*)\b"
)
_OBSERVABLE_CUES = {
    "visual": re.compile(
        r"(?i)inspect|look|visible|leak|wear|rust|corrosion|осмотр|теч|износ|ржав"
    ),
    "sound": re.compile(r"(?i)noise|sound|rattle|knock|squeak|шум|звук|стук|скрип"),
    "feel": re.compile(r"(?i)vibrat|shak|jerk|feel|вибрац|тряск|рывок"),
    "test_drive": re.compile(r"(?i)driv|road|accelerat|shift|езд|ходу|разгон|переключ"),
    "cold_start": re.compile(r"(?i)cold.?start|start.?up|запуск|холодн"),
    "controls": re.compile(
        r"(?i)warning|display|dash|indicator|switch|control|прибор|индикатор|кноп"
    ),
    "documents": re.compile(r"(?i)histor|record|service book|VIN|документ|истори|книжк"),
    "under_hood": re.compile(r"(?i)hood|bonnet|engine bay|hose|под капот|мотор|двигател|шланг"),
}
_MAINTENANCE_CUES = re.compile(
    r"(?i)service|maintenan|interval|fluid|oil|filter|belt|history|record|обслуж|регламент|замен|масл|жидк|фильтр|ремень|истори"
)
_INTERVALS = re.compile(r"(?i)\b\d[\d\s]*(?:тыс\.?\s*)?(?:км|km|mile|миль|лет|год|месяц|мес\.)\b")
_VAGUE_CHECK = re.compile(
    r"(?i)^\s*(?:проверьте|проверить|осмотрите|осмотреть)\s+"
    r"(?:двигатель|подвеску|электронику|кузов|трансмиссию|тормоза)\s*[.!]?\s*$"
)


class ClaimDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=4, max_length=200)
    description: str = Field(min_length=8, max_length=1000)
    system_name: str = Field(default="", max_length=100)
    issue_summary: str = Field(default="", max_length=500)
    inspection_category: InspectionCategory = "other"
    why_it_matters: str = Field(default="", max_length=500)
    what_to_check: list[str] = Field(default_factory=list, max_length=5)
    how_to_check: list[str] = Field(default_factory=list, max_length=5)
    service_history_checks: list[str] = Field(default_factory=list, max_length=5)
    component: str = Field(default="", max_length=100)
    evidence_refs: list[int] = Field(min_length=1)
    buyer_checks: list[str] = Field(default_factory=list, max_length=4)
    inspection_methods: list[InspectionMethod] = Field(default_factory=list)
    warning_signs: list[str] = Field(default_factory=list, max_length=4)
    requires_service: bool = False
    service_note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_inspection(self) -> "ClaimDraft":
        if self.requires_service and not self.service_note:
            raise ValueError("Service-only item needs a service note")
        if self.service_note and not self.requires_service:
            raise ValueError("Service note requires service-only flag")
        if self.requires_service and "service_required" not in self.inspection_methods:
            raise ValueError("Service-only item needs service_required method")
        if "service_required" in self.inspection_methods and not self.requires_service:
            raise ValueError("service_required method needs service-only flag")
        return self


class ProfileDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    common_problems: list[ClaimDraft] = Field(default_factory=list)
    problematic_components: list[ClaimDraft] = Field(default_factory=list)
    inspection_points: list[ClaimDraft] = Field(default_factory=list)
    expensive_failures: list[ClaimDraft] = Field(default_factory=list)
    risk_summary: str = Field(default="", max_length=1500)
    risk_summary_evidence_refs: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_summary_evidence(self) -> "ProfileDraft":
        if self.risk_summary and not self.risk_summary_evidence_refs:
            raise ValueError("Risk summary needs evidence references")
        return self


class GroundedClaim(ClaimDraft):
    category: ClaimCategory
    scope_key: str
    support_source_count: int
    independent_source_count: int = 0
    support_document_count: int = 0
    scope_label: str = "model"
    evidence_summary: str = ""
    confidence: Confidence

    @field_validator("confidence", mode="before")
    @classmethod
    def migrate_low_confidence(cls, value: str) -> str:
        return "limited" if value == "low" else value

    @model_validator(mode="after")
    def cap_single_model_source(self) -> "GroundedClaim":
        if (
            self.scope_label == "model"
            and self.independent_source_count == 1
            and self.confidence == "medium"
        ):
            self.confidence = "limited"
        return self


class TechnicalProfile(BaseModel):
    status: Literal["complete", "insufficient_evidence"]
    profile_version: int = 1
    common_problems: list[GroundedClaim] = Field(default_factory=list)
    problematic_components: list[GroundedClaim] = Field(default_factory=list)
    inspection_points: list[GroundedClaim] = Field(default_factory=list)
    expensive_failures: list[GroundedClaim] = Field(default_factory=list)
    risk_summary: str = ""
    risk_summary_evidence_refs: list[int] = Field(default_factory=list)
    risk_summary_confidence: Confidence | None = None
    risk_summary_scope_label: str = ""
    confidence: Confidence = "limited"
    source_count: int = 0
    evidence_count: int = 0

    @field_validator("confidence", mode="before")
    @classmethod
    def migrate_low_confidence(cls, value: str) -> str:
        return "limited" if value == "low" else value

    @model_validator(mode="after")
    def cap_profile_to_claims(self) -> "TechnicalProfile":
        claims = (
            self.common_problems
            + self.problematic_components
            + self.inspection_points
            + self.expensive_failures
        )
        if claims and all(claim.confidence == "limited" for claim in claims):
            self.confidence = "limited"
        if (
            self.risk_summary_confidence == "medium"
            and self.risk_summary_scope_label == "model"
            and self.source_count <= 1
        ):
            self.risk_summary_confidence = "limited"
        return self


@dataclass(slots=True)
class ValidationCounts:
    claims_generated: int = 0
    claims_rejected_no_evidence: int = 0
    claims_rejected_scope_mismatch: int = 0


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    answer: str = Field(default="", max_length=1500)
    evidence_refs: list[int] = Field(default_factory=list)
    insufficient_evidence: bool = False

    @model_validator(mode="after")
    def require_citations(self) -> "AnswerDraft":
        if self.answer and not self.evidence_refs:
            raise ValueError("Nonempty answer requires evidence")
        if self.insufficient_evidence and self.answer:
            raise ValueError("Insufficient-evidence answer must be empty")
        return self


def validate_grounded_profile(
    draft: ProfileDraft,
    hits: list[KnowledgeHit],
    identity: KnowledgeIdentity,
    *,
    reject_invalid: bool = False,
    counts: ValidationCounts | None = None,
) -> TechnicalProfile:
    """Reject all invented, out-of-scope, or unreferenced claims before persistence."""
    allowed = {item.scope_key for item in identity.fallback()}
    valid_hits = {
        hit.chunk_id: hit
        for hit in hits
        if hit.scope_key in allowed and usable_hit(hit, identity, reranked=False)
    }
    scope_order = {scope.scope_key: index for index, scope in enumerate(identity.fallback())}
    groups: dict[str, list[GroundedClaim]] = defaultdict(list)
    used: set[int] = set()
    counts = counts if counts is not None else ValidationCounts()
    for category in (
        "common_problems",
        "problematic_components",
        "inspection_points",
        "expensive_failures",
    ):
        for claim in getattr(draft, category):
            counts.claims_generated += 1
            if len(set(claim.evidence_refs)) != len(claim.evidence_refs):
                counts.claims_rejected_no_evidence += 1
                if reject_invalid:
                    continue
                raise ValueError("Duplicate evidence references in claim")
            if not set(claim.evidence_refs) <= valid_hits.keys():
                if any(
                    hit.chunk_id in claim.evidence_refs and hit.scope_key not in allowed
                    for hit in hits
                ):
                    counts.claims_rejected_scope_mismatch += 1
                else:
                    counts.claims_rejected_no_evidence += 1
                if reject_invalid:
                    continue
                raise ValueError("Claim cites missing or incompatible evidence")
            evidence = [valid_hits[ref] for ref in claim.evidence_refs]
            text = " ".join(hit.content for hit in evidence)
            if category == "expensive_failures" and not _REPAIR_EVIDENCE.search(text):
                counts.claims_rejected_no_evidence += 1
                if reject_invalid:
                    continue
                raise ValueError("Expensive failure lacks cited repair or cost evidence")
            claim_text = " ".join(
                [
                    claim.title,
                    claim.description,
                    claim.system_name,
                    claim.issue_summary,
                    claim.why_it_matters,
                    claim.service_note,
                ]
                + claim.what_to_check
                + claim.how_to_check
                + claim.service_history_checks
                + claim.buyer_checks
                + claim.warning_signs
            )
            codes = set(_TECHNICAL_CODE.findall(claim_text))
            if not codes <= set(_TECHNICAL_CODE.findall(text)):
                counts.claims_rejected_no_evidence += 1
                if reject_invalid:
                    continue
                raise ValueError("Claim introduces a technical code absent from its evidence")
            used.update(claim.evidence_refs)
            broadest = max(evidence, key=lambda item: scope_order[item.scope_key])
            tier = grade_evidence(evidence, identity)
            kinds = {kind for hit in evidence for kind in hit.source_types}
            reason = (
                "Сообщения владельцев; независимое техническое подтверждение ограничено"
                if kinds and kinds <= {"owner_forum", "forum", "community", "owner_report"}
                else f"{len(source_domains(evidence))} независимых доменов, "
                f"{len({hit.document_id for hit in evidence})} документов; "
                f"область: {scope_label(broadest.scope_key, identity)}"
            )
            description = claim.description
            if (
                scope_label(broadest.scope_key, identity) == "model"
                and identity.canonical_generation_id
            ):
                description = (
                    f"Сведения о модели в целом, не о конкретном поколении/двигателе: {description}"
                )
            if kinds and kinds <= {"owner_forum", "forum", "community", "owner_report"}:
                description = (
                    "По отдельным сообщениям владельцев (не подтверждённый массовый дефект): "
                    f"{description}"
                )
            claim_fields = {**claim.model_dump(), "description": description[:1000]}
            if claim.service_history_checks and not _MAINTENANCE_CUES.search(text):
                claim_fields["service_history_checks"] = []
            else:
                claim_fields["service_history_checks"] = [
                    check
                    for check in claim.service_history_checks
                    if all(
                        interval.group().casefold() in text.casefold()
                        for interval in _INTERVALS.finditer(check)
                    )
                ]
            checks = [
                check
                for check in (claim.what_to_check or claim.buyer_checks)
                if not _VAGUE_CHECK.fullmatch(check)
            ]
            claim_fields["what_to_check"] = checks
            claim_fields["buyer_checks"] = checks
            observable = bool(checks or claim_fields["service_history_checks"]) and any(
                _OBSERVABLE_CUES[method].search(text)
                for method in claim.inspection_methods
                if method in _OBSERVABLE_CUES
            )
            if claim_fields["service_history_checks"] and "documents" in claim.inspection_methods:
                observable = True
            if not observable and not claim.requires_service:
                claim_fields.update(
                    buyer_checks=[],
                    what_to_check=[],
                    how_to_check=[],
                    warning_signs=[],
                    requires_service=True,
                    service_note=(
                        "Источник не описывает проверку, доступную при обычном осмотре; "
                        "для подтверждения обратитесь в независимый сервис."
                    ),
                    inspection_methods=["service_required"],
                )
            groups[category].append(
                GroundedClaim(
                    **claim_fields,
                    category=category,
                    scope_key=broadest.scope_key,
                    scope_label=scope_label(broadest.scope_key, identity),
                    support_source_count=len({url for hit in evidence for url in hit.source_urls}),
                    independent_source_count=len(source_domains(evidence)),
                    support_document_count=len({hit.document_id for hit in evidence}),
                    evidence_summary=reason,
                    confidence=tier,
                )
            )
    if draft.risk_summary:
        if not set(draft.risk_summary_evidence_refs) <= valid_hits.keys():
            counts.claims_rejected_no_evidence += 1
            if not reject_invalid:
                raise ValueError("Risk summary cites missing or incompatible evidence")
            draft = draft.model_copy(update={"risk_summary": "", "risk_summary_evidence_refs": []})
        else:
            summary_evidence = [valid_hits[ref] for ref in draft.risk_summary_evidence_refs]
            summary_codes = set(_TECHNICAL_CODE.findall(draft.risk_summary))
            evidenced_codes = set(
                _TECHNICAL_CODE.findall(" ".join(hit.content for hit in summary_evidence))
            )
            if not summary_codes <= evidenced_codes:
                counts.claims_rejected_no_evidence += 1
                if not reject_invalid:
                    raise ValueError(
                        "Risk summary introduces a technical code absent from evidence"
                    )
                draft = draft.model_copy(
                    update={"risk_summary": "", "risk_summary_evidence_refs": []}
                )
            else:
                used.update(draft.risk_summary_evidence_refs)
    if not used:
        return TechnicalProfile(status="insufficient_evidence")
    contributing = [valid_hits[chunk_id] for chunk_id in used]
    summary_evidence = [valid_hits[chunk_id] for chunk_id in draft.risk_summary_evidence_refs]
    summary_scope = (
        max(summary_evidence, key=lambda item: scope_order[item.scope_key]).scope_key
        if summary_evidence
        else ""
    )
    return TechnicalProfile(
        status="complete",
        profile_version=2,
        common_problems=groups["common_problems"],
        problematic_components=groups["problematic_components"],
        inspection_points=groups["inspection_points"],
        expensive_failures=groups["expensive_failures"],
        risk_summary=draft.risk_summary,
        risk_summary_evidence_refs=draft.risk_summary_evidence_refs,
        risk_summary_confidence=(
            grade_evidence(summary_evidence, identity) if summary_evidence else None
        ),
        risk_summary_scope_label=scope_label(summary_scope, identity) if summary_scope else "",
        confidence=grade_evidence(contributing, identity),
        source_count=len(source_domains(contributing)),
        evidence_count=len(used),
    )


def validate_grounded_answer(
    answer: AnswerDraft, hits: list[KnowledgeHit], identity: KnowledgeIdentity
) -> AnswerDraft:
    allowed = {scope.scope_key for scope in identity.fallback()}
    evidence = {hit.chunk_id: hit for hit in hits if hit.scope_key in allowed}
    if not set(answer.evidence_refs) <= evidence.keys():
        raise ValueError("Answer cites missing or incompatible evidence")
    source_text = " ".join(evidence[ref].content for ref in answer.evidence_refs)
    if not set(_TECHNICAL_CODE.findall(answer.answer)) <= set(_TECHNICAL_CODE.findall(source_text)):
        raise ValueError("Answer introduces a technical code absent from evidence")
    return answer
