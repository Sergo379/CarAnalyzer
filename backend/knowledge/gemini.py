"""Grounded Gemini adapter. API keys and raw provider errors never enter diagnostics."""

import asyncio
import json
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from urllib.parse import urlsplit

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.config import Settings, get_settings
from backend.knowledge.identity import KnowledgeIdentity
from backend.knowledge.profile import AnswerDraft, ProfileDraft
from backend.knowledge.retrieval import KnowledgeHit
from backend.models.car import BodyType
from backend.services.segment_classifier import SegmentClassification

_RUSSIAN_NARRATIVE = re.compile(r"[А-Яа-яЁё]")
logger = logging.getLogger(__name__)
ProviderProgress = Callable[[str, dict[str, object]], None]


def _russian_buyout_narrative(draft: ProfileDraft) -> bool:
    for group in (
        draft.common_problems,
        draft.problematic_components,
        draft.inspection_points,
        draft.expensive_failures,
    ):
        for claim in group:
            narrative = [
                claim.title,
                claim.description,
                claim.issue_summary,
                claim.why_it_matters,
                claim.service_note,
                *claim.what_to_check,
                *claim.how_to_check,
                *claim.service_history_checks,
                *claim.buyer_checks,
                *claim.warning_signs,
            ]
            if any(text and not _RUSSIAN_NARRATIVE.search(text) for text in narrative):
                return False
    return not draft.risk_summary or bool(_RUSSIAN_NARRATIVE.search(draft.risk_summary))


class ProviderNotConfiguredError(RuntimeError):
    pass


class ProviderGenerationError(RuntimeError):
    def __init__(
        self,
        category: str,
        *,
        retryable: bool = False,
        detail: str = "",
        validation_errors: list[dict[str, str]] | None = None,
    ) -> None:
        self.category = category
        self.retryable = retryable
        self.detail = detail
        self.validation_errors = validation_errors or []
        super().__init__(category)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    draft: ProfileDraft
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    prompt_version: str


@dataclass(frozen=True, slots=True)
class AnswerResult:
    draft: AnswerDraft
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    prompt_version: str


class SegmentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_code: str
    segment_family: str
    market_position: str


def _usage(response: object) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None)
    return (
        int(getattr(usage, "prompt_token_count", 0) or 0),
        int(getattr(usage, "candidates_token_count", 0) or 0),
    )


class GeminiProvider:
    max_attempts = 3

    def __init__(self, settings: Settings | None = None, client: object | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = client

    @property
    def configured(self) -> bool:
        key = self.settings.gemini_api_key
        return bool(key and key.strip() and key != "YOUR_KEY_HERE")

    async def _generate(
        self,
        contents: str,
        config: types.GenerateContentConfig,
        progress: ProviderProgress | None = None,
    ) -> object:
        if not self.configured:
            raise ProviderNotConfiguredError("Gemini provider is not configured")
        client = self.client or genai.Client(
            api_key=self.settings.gemini_api_key,
            http_options=types.HttpOptions(
                timeout=int(self.settings.gemini_timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        config = config.model_copy(
            update={
                "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
            }
        )
        timeout = self.settings.gemini_timeout_seconds
        provider_started = datetime.now(UTC).isoformat()

        def notify(phase: str, attempt: int, **fields: object) -> None:
            if progress:
                progress(
                    phase,
                    {
                        "provider": "gemini",
                        "provider_attempt": attempt,
                        "provider_max_attempts": self.max_attempts,
                        "provider_timeout_seconds": timeout,
                        "provider_started_at": provider_started,
                        **fields,
                    },
                )

        try:
            for attempt in range(self.max_attempts):
                notify(
                    "calling_gemini",
                    attempt + 1,
                    provider_attempt_started_at=datetime.now(UTC).isoformat(),
                    retry_in_seconds=0.0,
                    retry_started_at="",
                )
                logger.info(
                    "gemini attempt=%s/%s timeout=%ss started",
                    attempt + 1,
                    self.max_attempts,
                    timeout,
                )
                retry_reason = ""
                try:
                    response = await asyncio.wait_for(
                        client.aio.models.generate_content(
                            model=self.settings.gemini_model, contents=contents, config=config
                        ),
                        timeout=timeout,
                    )
                    notify(
                        "validating_gemini_response",
                        attempt + 1,
                        sdk_response_received=True,
                        retry_in_seconds=0.0,
                    )
                    logger.info("gemini attempt=%s SDK response received", attempt + 1)
                    return response
                except errors.APIError as exc:
                    code = getattr(exc, "code", None)
                    if code in {500, 502, 503, 504} and attempt < self.max_attempts - 1:
                        retry_reason = str(code)
                    elif code in {500, 502, 503, 504}:
                        raise ProviderGenerationError(
                            "provider_unavailable", retryable=True
                        ) from None
                    if code == 429:
                        raise ProviderGenerationError("rate_limited", retryable=True) from None
                    if code in {401, 403}:
                        raise ProviderGenerationError("authentication_failed") from None
                    if code == 400:
                        raise ProviderGenerationError("invalid_request") from None
                    if code == 404:
                        raise ProviderGenerationError("model_unavailable") from None
                    if not retry_reason:
                        raise ProviderGenerationError("provider_error") from None
                except (httpx.TimeoutException, TimeoutError):
                    # Do not repeat a full expired timeout budget three times.
                    logger.info("gemini attempt=%s provider_timeout", attempt + 1)
                    raise ProviderGenerationError("timeout", retryable=True) from None
                except httpx.TransportError:
                    raise ProviderGenerationError("network_error", retryable=True) from None
                delay = 0.5 * (2**attempt) + random.uniform(0, 0.25)
                notify(
                    "provider_retry",
                    attempt + 2,
                    retry_in_seconds=delay,
                    retry_started_at=datetime.now(UTC).isoformat(),
                )
                logger.info(
                    "gemini retry reason=%s next_attempt=%s/%s delay=%.2fs",
                    retry_reason,
                    attempt + 2,
                    self.max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)
        finally:
            if self.client is None:
                try:
                    await asyncio.wait_for(client.aio.aclose(), timeout=2.0)
                except Exception as exc:
                    logger.info("gemini cleanup error=%s", type(exc).__name__)
                finally:
                    try:
                        await asyncio.wait_for(asyncio.to_thread(client.close), timeout=2.0)
                    except Exception as exc:
                        logger.info("gemini sync cleanup error=%s", type(exc).__name__)
        raise ProviderGenerationError("provider_unavailable", retryable=True)

    async def classify_segment(
        self, metadata: dict, body: BodyType | None
    ) -> SegmentClassification | None:
        """Last-resort structured classification; caller enforces scope and timeout."""
        response = await self._generate(
            json.dumps({"body_type": body.value if body else None, "metadata": metadata}),
            types.GenerateContentConfig(
                system_instruction=(
                    "Classify the vehicle using ONLY supplied catalog metadata. "
                    "Return UNKNOWN when dimensions and source class are insufficient or conflict. "
                    "Valid segment_code: A,B,C,D,E,F,J-B,J-C,J-D,J-E,J-F,M,S,UNKNOWN; "
                    "families: passenger,suv,mpv,sport,unknown; "
                    "market_position: mainstream,premium,luxury,unknown."
                ),
                response_mime_type="application/json",
                response_json_schema=SegmentDraft.model_json_schema(),
                temperature=0,
            ),
        )
        try:
            draft = SegmentDraft.model_validate_json(str(getattr(response, "text", "")))
        except ValidationError:
            return None
        if draft.segment_code not in {
            "A",
            "B",
            "C",
            "D",
            "E",
            "F",
            "J-B",
            "J-C",
            "J-D",
            "J-E",
            "J-F",
            "M",
            "S",
        } or draft.market_position not in {"mainstream", "premium", "luxury", "unknown"}:
            return None
        return SegmentClassification(
            segment_code=draft.segment_code,
            segment_family=draft.segment_family,
            segment_size=draft.segment_code[-1] if draft.segment_code[-1] in "ABCDEF" else None,
            market_position=draft.market_position,
            segment_method="ai_fallback",
            segment_confidence="low",
        )

    async def generate_profile(
        self,
        identity: KnowledgeIdentity,
        hits: list[KnowledgeHit],
        prompt_version: str = "buyout-inspection-v4",
        *,
        progress: ProviderProgress | None = None,
    ) -> GenerationResult:
        if not hits:
            raise ProviderGenerationError("no_evidence")
        evidence = [
            {
                "chunk_id": hit.chunk_id,
                "scope_key": hit.scope_key,
                "domain": sorted(
                    {urlsplit(url).hostname for url in hit.source_urls if urlsplit(url).hostname}
                ),
                "source_type": hit.source_types,
                "evidence_tier": hit.evidence_tier,
                "text": hit.content[:1100],
            }
            for hit in hits[:6]
        ]
        instruction = (
            "Ты опытный специалист по выкупу подержанных автомобилей. Пиши по-русски; "
            "Оригинальные названия узлов и английские термины сохраняй в скобках. "
            "Используй ТОЛЬКО приведённые фрагменты. Каждое утверждение и практический совет "
            "должны ссылаться на реальные chunk_id. Не придумывай неисправности, частоту, коды, "
            "сроки обслуживания, процедуры проверки или стоимость. "
            "Отделяй сообщения владельцев от подтверждённых "
            "типовых дефектов; общие данные модели не называй специфичными для мотора. "
            "Приоритет — конкретные действия профессионального покупателя рядом с машиной: "
            "что именно проверить, где и как, какие наблюдаемые признаки настораживают и почему. "
            "У каждого пункта заполни system_name, inspection_category, issue_summary, "
            "why_it_matters, what_to_check, how_to_check и warning_signs, если подтверждено. "
            "Категории: body, suspension, steering, engine, cooling, transmission, drivetrain, "
            "brakes, electronics, interior, service_history, other. Ищи полезные проверки кузова, "
            "подвески, холодного запуска, трансмиссии, тормозов, электроники "
            "и пространства под капотом. "
            "В service_history_checks записывай только специфичное подтверждённое обслуживание; "
            "если интервалы не указаны в доказательствах, не указывай их. "
            "Доступные методы: звук, ощущения, тест-драйв, холодный запуск, визуальный осмотр, "
            "органы управления, документы, под капотом. Для невидимых "
            "неисправностей укажи requires_service=true и service_note; не выдавай специальную "
            "диагностику за самостоятельную проверку. В buyer_checks используй только проверяемые "
            "шаги, поддержанные источником; warning_signs — только подтверждённые признаки. "
            "inspection_methods — только значения visual, sound, feel, test_drive, "
            "cold_start, controls, "
            "documents, under_hood, service_required. Ограничь каждый раздел двумя пунктами. "
            "Если доказательств нет, верни пустые разделы и пустую сводку. "
            "Сводка риска должна ссылаться на доказательства, без общей оценки надёжности."
        )
        contents = json.dumps(
            {
                "vehicle": {
                    "brand": identity.brand,
                    "model": identity.model,
                    "generation": identity.generation,
                    "engine": identity.engine,
                },
                "evidence": evidence,
            },
            ensure_ascii=False,
        )
        started = perf_counter()
        response = await self._generate(
            contents,
            types.GenerateContentConfig(
                system_instruction=instruction,
                response_mime_type="application/json",
                response_json_schema=ProfileDraft.model_json_schema(),
            ),
            progress=progress,
        )
        input_tokens, output_tokens = _usage(response)
        if progress:
            progress(
                "validating_gemini_response",
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "llm_latency_ms": int((perf_counter() - started) * 1000),
                    "sdk_response_received": True,
                    "structured_validation_status": "validating",
                },
            )
        try:
            draft = ProfileDraft.model_validate_json(response.text or "")
        except ValidationError as exc:
            validation_errors = [
                {
                    "path": ".".join(str(part) for part in error["loc"]) or "$",
                    "type": error["type"],
                    "message": error["msg"],
                }
                for error in exc.errors(
                    include_input=False, include_context=False, include_url=False
                )
            ]
            logger.warning(
                "gemini ProfileDraft validation failed errors=%s",
                json.dumps(validation_errors, ensure_ascii=False),
            )
            raise ProviderGenerationError(
                "invalid_structured_output",
                detail="schema_validation",
                validation_errors=validation_errors,
            ) from None
        except (ValueError, TypeError):
            raise ProviderGenerationError(
                "invalid_structured_output", detail="schema_validation"
            ) from None
        if not _russian_buyout_narrative(draft):
            raise ProviderGenerationError(
                "invalid_structured_output", detail="non_russian_narrative"
            )
        return GenerationResult(
            draft,
            str(getattr(response, "model_version", None) or self.settings.gemini_model),
            input_tokens,
            output_tokens,
            int((perf_counter() - started) * 1000),
            prompt_version,
        )

    async def answer_question(
        self,
        identity: KnowledgeIdentity,
        question: str,
        hits: list[KnowledgeHit],
        prompt_version: str = "grounded-v1",
    ) -> AnswerResult:
        if not hits:
            return AnswerResult(
                AnswerDraft(insufficient_evidence=True),
                self.settings.gemini_model,
                0,
                0,
                0,
                prompt_version,
            )
        started = perf_counter()
        response = await self._generate(
            json.dumps(
                {
                    "vehicle": identity.scope_key,
                    "question": question,
                    "evidence": [
                        {"chunk_id": hit.chunk_id, "text": hit.content[:1500]} for hit in hits[:10]
                    ],
                },
                ensure_ascii=False,
            ),
            types.GenerateContentConfig(
                system_instruction=(
                    "Ответь только из приведённых доказательств в JSON. "
                    "Если ответа нет, оставь answer и evidence_refs пустыми. "
                    "Установи insufficient_evidence=true. "
                    "Не выдумывай факты или ссылки."
                ),
                response_mime_type="application/json",
                response_json_schema=AnswerDraft.model_json_schema(),
            ),
        )
        try:
            draft = AnswerDraft.model_validate_json(response.text or "")
        except (ValidationError, ValueError, TypeError):
            raise ProviderGenerationError("invalid_structured_output") from None
        input_tokens, output_tokens = _usage(response)
        return AnswerResult(
            draft,
            str(getattr(response, "model_version", None) or self.settings.gemini_model),
            input_tokens,
            output_tokens,
            int((perf_counter() - started) * 1000),
            prompt_version,
        )
