import { useEffect, useRef, useState } from "react";

export interface KnowledgeSelection {
  brand: string;
  model: string;
  generation_id: string;
  engine_id: string;
}

interface GroundedClaim {
  title: string;
  description: string;
  system_name?: string;
  issue_summary?: string;
  inspection_category?: string;
  why_it_matters?: string;
  what_to_check?: string[];
  how_to_check?: string[];
  service_history_checks?: string[];
  component: string;
  scope_key: string;
  evidence_refs: number[];
  support_source_count: number;
  independent_source_count?: number;
  support_document_count?: number;
  scope_label?: "model" | "generation" | "engine" | "modification";
  evidence_summary?: string;
  confidence: "low" | "limited" | "medium" | "high";
  buyer_checks?: string[];
  inspection_methods?: string[];
  warning_signs?: string[];
  requires_service?: boolean;
  service_note?: string;
}

interface TechnicalProfile {
  status: "complete" | "insufficient_evidence";
  profile_version?: number;
  common_problems: GroundedClaim[];
  problematic_components: GroundedClaim[];
  inspection_points: GroundedClaim[];
  expensive_failures: GroundedClaim[];
  risk_summary: string;
  risk_summary_confidence?: "low" | "limited" | "medium" | "high" | null;
  risk_summary_scope_label?: "model" | "generation" | "engine" | "modification";
  confidence: "low" | "limited" | "medium" | "high";
  source_count: number;
  evidence_count: number;
}

interface KnowledgeResponse {
  status: string;
  phase?: string;
  scope_key: string;
  scope_level?: "model" | "generation" | "engine" | "modification";
  profile?: TechnicalProfile | null;
  sources_discovered?: number;
  sources_loaded?: number;
  chunks_created?: number;
  embeddings_created?: number;
  diagnostics?: { knowledge_path?: string; discovery_reason?: string; sources_failed?: number };
  knowledge_path?: string;
  discovery_reason?: string;
  sources_failed?: number;
  build_elapsed_seconds?: number;
  elapsed_seconds?: number;
  provider?: string;
  provider_attempt?: number;
  provider_max_attempts?: number;
  provider_elapsed_seconds?: number;
  provider_timeout_seconds?: number;
  retry_in_seconds?: number;
}

const completedStatuses = new Set(["complete", "partial", "cached", "rebuild_preserved"]);
const activeStatuses = new Set(["building", "queued", "calling_gemini", "provider_retry", "generating", "retrieving", "reranking", "validating", "saving"]);

function requestErrorMessage(reason: unknown): string {
  if (reason instanceof DOMException && reason.name === "TimeoutError") return "истекло время ожидания ответа";
  if (reason instanceof TypeError) return "нет связи с сервером";
  if (reason instanceof Error && /^HTTP \d{3}$/.test(reason.message)) {
    return `сервер ответил ${reason.message}`;
  }
  return "не удалось связаться с сервером";
}

interface KnowledgeSource {
  url: string;
  title: string;
  domain: string;
  source_type: string;
  status: string;
  chunk_ids: number[];
}

const confidenceLabels = {
  low: "Ограниченные данные", limited: "Ограниченные данные",
  medium: "Средняя подтверждённость", high: "Высокая подтверждённость",
};
const scopeLabels = {
  model: "модель в целом", generation: "поколение", engine: "конкретный двигатель", modification: "модификация",
};
const phaseLabels: Record<string, string> = {
  queued: "подготовка",
  checking_local_corpus: "проверка локальных знаний",
  checking_profile_cache: "проверка сохранённого профиля",
  discovering_sources: "поиск источников",
  loading_documents: "загрузка материалов",
  processing_documents: "обработка данных",
  chunking: "обработка данных",
  embedding: "построение индекса",
  indexing: "построение индекса",
  retrieving: "поиск подтверждений",
  retrieving_vectors: "подбор релевантных фрагментов",
  reranking: "проверка релевантности",
  evaluating_evidence: "оценка подтверждений",
  generating: "формирование результата",
  calling_gemini: "Gemini анализирует технические данные",
  provider_retry: "Gemini временно недоступен",
  validating_gemini_response: "проверка формата ответа Gemini",
  validating: "проверка результата",
  saving: "сохранение результата",
};

function KnowledgeProgress({ response }: { response: KnowledgeResponse }) {
  const [now, setNow] = useState(Date.now());
  const observed = useRef({ response, at: Date.now() });
  if (observed.current.response !== response) observed.current = { response, at: Date.now() };
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const delta = Math.max(0, (now - observed.current.at) / 1000);
  const phase = response.phase ?? response.status;
  return <div className="knowledge-progress" role="status">
    <p>Собираем техническую информацию…</p>
    <p>{phaseLabels[phase] ?? "Обрабатываем технические данные"}</p>
    <small>Всего прошло {Math.floor((response.build_elapsed_seconds ?? 0) + delta)} сек</small>
    {phase === "calling_gemini" && <small>
      Попытка {response.provider_attempt ?? 1} из {response.provider_max_attempts ?? 3} · прошло {Math.floor((response.provider_elapsed_seconds ?? 0) + delta)} сек
      {!!response.provider_timeout_seconds && ` · таймаут ${response.provider_timeout_seconds} сек`}
    </small>}
    {phase === "provider_retry" && <small>
      Повторная попытка через {Math.ceil(Math.max(0, (response.retry_in_seconds ?? 0) - delta))} сек · попытка {response.provider_attempt} из {response.provider_max_attempts}
    </small>}
    {(response.knowledge_path ?? response.diagnostics?.knowledge_path) === "discovery" &&
      <small>Локальных подтверждений недостаточно; ищем внешние технические источники.</small>}
  </div>;
}
const methodLabels: Record<string, string> = {
  visual: "осмотр", sound: "звук", feel: "ощущения", test_drive: "тест-драйв",
  cold_start: "холодный запуск",
  controls: "органы управления", documents: "документы", under_hood: "под капотом",
  service_required: "нужен сервис",
};
const sourceTypeLabels: Record<string, string> = {
  official: "официальный материал", repair: "материал по ремонту",
  recall: "отзывная кампания", technical_article: "техническая статья",
  owner_forum: "сообщество владельцев", forum: "форум",
  community: "сообщество", owner_report: "отчёт владельца",
};
const sourceStatusLabels: Record<string, string> = {
  complete: "загружен", blocked: "доступ ограничен", timeout: "не ответил вовремя",
  parse_error: "формат не распознан",
};

function GeneralChecklist() {
  return <div className="knowledge-group"><h3>Базовая проверка при выкупе</h3>
    <p>Общий чек-лист, не относится к подтверждённым особенностям выбранной модели.</p>
    <ul><li>Осмотрите кузов, салон и доступные агрегаты при хорошем освещении.</li>
      <li>На тест-драйве обратите внимание на посторонние звуки, вибрации и работу органов управления.</li>
      <li>Сверьте документы и историю обслуживания; при сомнениях организуйте независимую диагностику.</li></ul>
  </div>;
}

function sourceCountLabel(count: number) {
  const lastTwo = count % 100;
  const last = count % 10;
  if (lastTwo < 11 || lastTwo > 14) {
    if (last === 1) return `${count} независимый источник`;
    if (last >= 2 && last <= 4) return `${count} независимых источника`;
  }
  return `${count} независимых источников`;
}

function ClaimGroup({ title, claims }: { title: string; claims: GroundedClaim[] }) {
  if (!claims?.length) return null;
  return <div className="knowledge-group"><h3>{title}</h3><ul>{claims.map((claim) => (
    <li key={`${claim.title}-${claim.scope_key}`}>
      <strong>{claim.system_name ? `${claim.system_name}: ` : ""}{claim.title}</strong>
      <p><b>Что известно:</b> {claim.issue_summary || claim.description}</p>
      {claim.why_it_matters && <p><b>Почему важно:</b> {claim.why_it_matters}</p>}
      <small>{confidenceLabels[claim.confidence]} · {scopeLabels[claim.scope_label ?? "model"]} · {sourceCountLabel(claim.independent_source_count ?? claim.support_source_count)}</small>
      {!!(claim.what_to_check?.length || claim.buyer_checks?.length) && <div><b>Что проверить:</b><ul>{(claim.what_to_check?.length ? claim.what_to_check : claim.buyer_checks ?? []).map((check) => <li key={check}>{check}</li>)}</ul></div>}
      {!!claim.warning_signs?.length && <div><b>Что должно насторожить:</b><ul>{claim.warning_signs.map((sign) => <li key={sign}>{sign}</li>)}</ul></div>}
      {!!claim.how_to_check?.length && <div><b>Как проверить:</b><ul>{claim.how_to_check.map((step) => <li key={step}>{step}</li>)}</ul></div>}
      {!!claim.inspection_methods?.length && <small>Метод: {claim.inspection_methods.map((method) => methodLabels[method] ?? method).join(", ")}</small>}
      {claim.requires_service && claim.service_note && <p>Проверка в сервисе: {claim.service_note}</p>}
    </li>
  ))}</ul></div>;
}

const inspectionGroups: { title: string; categories: string[] }[] = [
  { title: "Кузов и состояние", categories: ["body", "interior"] },
  { title: "Подвеска и рулевое", categories: ["suspension", "steering"] },
  { title: "Двигатель и холодный запуск", categories: ["engine", "cooling"] },
  { title: "Трансмиссия / привод", categories: ["transmission", "drivetrain"] },
  { title: "Тормоза / колёса", categories: ["brakes"] },
  { title: "Электроника", categories: ["electronics"] },
  { title: "Под капотом", categories: ["under_hood"] },
];

function InspectionSections({ profile }: { profile: TechnicalProfile }) {
  const all = [...(profile.common_problems ?? []), ...(profile.problematic_components ?? []),
    ...(profile.inspection_points ?? []), ...(profile.expensive_failures ?? [])];
  const customer = all.filter((item) => !item.requires_service);
  const categorized = new Set(inspectionGroups.flatMap((group) => group.categories));
  const underHood = (item: GroundedClaim) => item.inspection_category === "other" &&
    (item.inspection_methods ?? []).includes("under_hood");
  const history = all.flatMap((item) => (item.service_history_checks ?? []).map((check) => ({ check, item })));
  return <>
    {inspectionGroups.map((group) => <ClaimGroup key={group.title} title={group.title} claims={customer.filter((item) => group.categories.includes(item.inspection_category ?? "") || (group.title === "Под капотом" && underHood(item)))} />)}
    <ClaimGroup title="Другие подтверждённые особенности" claims={customer.filter((item) => !categorized.has(item.inspection_category ?? "other") && item.inspection_category !== "service_history" && !underHood(item))} />
    {history.length > 0 && <div className="knowledge-group"><h3>Что проверить по истории обслуживания</h3><ul>{history.map(({ check, item }) =>
      <li key={`${item.title}-${check}`}>{check} <small>{confidenceLabels[item.confidence]} · {sourceCountLabel(item.independent_source_count ?? item.support_source_count)}</small></li>
    )}</ul></div>}
    <ClaimGroup title="История обслуживания" claims={customer.filter((item) => item.inspection_category === "service_history" && !(item.service_history_checks?.length))} />
    <ClaimGroup title="Что проверить на сервисе" claims={all.filter((item) => item.requires_service)} />
  </>;
}

export function TechnicalKnowledge({ selection }: { selection: KnowledgeSelection }) {
  const [response, setResponse] = useState<KnowledgeResponse | null>(null);
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [startingBuild, setStartingBuild] = useState(false);
  const startBuildRef = useRef<(force?: boolean) => void>(() => {});
  const requestGeneration = useRef(0);
  const key = JSON.stringify(selection);

  useEffect(() => {
    const generation = ++requestGeneration.current;
    let cancelled = false;
    let timer: number | undefined;
    const controller = new AbortController();
    let buildStarting = false;
    const isCurrent = () => !cancelled && requestGeneration.current === generation;
    const params = new URLSearchParams({ brand: selection.brand, model: selection.model });
    if (selection.generation_id) params.set("generation_id", selection.generation_id);
    if (selection.engine_id) params.set("engine_id", selection.engine_id);
    const query = params.toString();
    const buildSelection = { ...selection, generation_id: selection.generation_id || null,
      engine_id: selection.engine_id || null };

    async function read(path: string, options?: RequestInit): Promise<KnowledgeResponse> {
      const result = await fetch(path, { ...options, signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]) });
      if (!result.ok) throw new Error(`HTTP ${result.status}`);
      return result.json() as Promise<KnowledgeResponse>;
    }

    async function refreshSources() {
      const sourceResponse = await fetch(`/api/knowledge/sources?${query}`, {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
      });
      if (sourceResponse.ok && isCurrent()) {
        const payload = await sourceResponse.json() as { sources: KnowledgeSource[] };
        if (isCurrent()) setSources(payload.sources);
      }
    }

    async function update(data: KnowledgeResponse, refreshProfile = false) {
      if (!isCurrent()) return;
      let next = data;
      if (refreshProfile || (completedStatuses.has(data.status) && !data.profile)) {
        const completed = await read(`/api/knowledge/profile?${query}`);
        if (!isCurrent()) return;
        if (completed.profile) {
          next = {
            ...data,
            ...completed,
            status: completed.profile.status,
            phase: completed.profile.status,
          };
        }
      }
      if (!isCurrent()) return;
      setError(null);
      setResponse((current) => !next.profile && current?.profile && !completedStatuses.has(next.status)
        ? { ...next, profile: current.profile, scope_level: current.scope_level }
        : next);
      if (activeStatuses.has(next.status) && isCurrent()) {
        if (timer) window.clearTimeout(timer);
        timer = window.setTimeout(poll, 3000);
      }
      if (next.profile) {
        try { await refreshSources(); } catch { /* Sources must not stop profile polling/rendering. */ }
      }
    }

    async function poll() {
      try {
        const status = await read(`/api/knowledge/status?${query}`);
        await update(status, completedStatuses.has(status.status));
      } catch (reason) {
        if (isCurrent()) {
          setError(requestErrorMessage(reason));
          // A transient status/profile fetch failure is not a terminal build state.
          timer = window.setTimeout(poll, 3000);
        }
      }
    }

    async function start() {
      try {
        if (!isCurrent()) return;
        setResponse(null); setSources([]); setError(null); setStartingBuild(false);
        const existing = await read(`/api/knowledge/profile?${query}`);
        if (!isCurrent()) return;
        await update(existing);
      } catch (reason) {
        if (isCurrent()) {
          setError(requestErrorMessage(reason));
        }
      }
    }

    async function beginBuild(force = false) {
      if (!isCurrent() || buildStarting) return;
      buildStarting = true;
      setStartingBuild(true);
      setError(null);
      try {
        const building = await read(force ? "/api/knowledge/rebuild" : "/api/knowledge/build", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(buildSelection),
        });
        await update(building);
      } catch (reason) {
        if (isCurrent()) {
          setError(requestErrorMessage(reason));
        }
      } finally {
        buildStarting = false;
        if (isCurrent()) setStartingBuild(false);
      }
    }

    startBuildRef.current = (force) => { void beginBuild(force); };
    void start();
    return () => {
      cancelled = true;
      controller.abort();
      startBuildRef.current = () => {};
      if (timer) window.clearTimeout(timer);
    };
  }, [key, retry]);

  const profile = response?.profile;
  return <section className="knowledge" aria-live="polite">
    <h2>Техническая проверка при выкупе</h2>
    <p className="eyebrow">{selection.brand} {selection.model}</p>
    {error && <p>Технический анализ сейчас недоступен: {error}. Рыночные результаты не затронуты; попробуйте позже.</p>}
    {error && !response && <button type="button" onClick={() => setRetry((value) => value + 1)}>Повторить проверку профиля</button>}
    {!error && !response && <p>Проверяем локальный профиль…</p>}
    {response && activeStatuses.has(response.status) && <KnowledgeProgress response={response} />}
    {response?.status === "not_found" && <p>Сохранённого технического профиля пока нет. Анализ запускается только по вашему запросу.</p>}
    {response?.status === "fallback_cached" && <p>Показаны общие данные; точный анализ выбранной конфигурации можно запустить вручную.</p>}
    {response?.status === "provider_not_configured" && <p>Провайдер Gemini не настроен. Рыночный поиск работает независимо; неподтверждённые технические проблемы не показываются.</p>}
    {response?.status === "no_sources" && <p>Технические источники для этого автомобиля не найдены.</p>}
    {response?.status === "blocked" && <p>Источник ограничил автоматический доступ. Проверку можно повторить позднее вручную.</p>}
    {response?.status === "timeout" && <p>Источник технических данных не ответил вовремя.</p>}
    {response?.status === "parse_error" && <p>Формат технического источника не распознан.</p>}
    {response?.status === "search_provider_unavailable" && <p>Сервис поиска технических источников временно недоступен. Отсутствие данных не подтверждено.</p>}
    {response?.status === "failed" && <p>Сбор знаний завершился ошибкой. Данные рынка не затронуты.</p>}
    {response?.status === "provider_rate_limited" && <p>Gemini временно ограничил запросы. Попробуйте позже.</p>}
    {response?.status === "provider_temporarily_unavailable" && <p>Gemini временно недоступен. Сохранённые источники останутся доступны для повторной попытки.</p>}
    {response?.status === "provider_timeout" && <p>Технический анализ Gemini превысил время ожидания. Локальные данные сохранены; анализ можно повторить.</p>}
    {response?.status === "provider_auth_error" && <p>Не удалось авторизовать Gemini. Проверьте локальную настройку ключа.</p>}
    {response?.status === "provider_invalid_request" && <p>Gemini отклонил запрос. Проверьте настройки модели и повторите позже.</p>}
    {response?.status === "provider_model_unavailable" && <p>Выбранная модель Gemini недоступна для этого ключа.</p>}
    {response?.status === "invalid_response" && <p>Gemini вернул некорректный формат ответа; профиль не сохранён.</p>}
    {response?.status === "structured_validation_failed" && <p>Ответ Gemini не прошёл проверку подтверждений; профиль не сохранён.</p>}
    {response && !activeStatuses.has(response.status) && (
      ["not_found", "fallback_cached", "provider_not_configured", "no_sources", "insufficient_evidence",
        "blocked", "timeout", "parse_error", "search_provider_unavailable", "failed",
        "provider_rate_limited", "provider_temporarily_unavailable", "provider_timeout",
        "provider_auth_error", "provider_invalid_request", "provider_model_unavailable",
        "invalid_response", "structured_validation_failed"].includes(response.status) ||
      (response.status === "cached" && profile?.profile_version === 1)
    ) && <button type="button" disabled={startingBuild} onClick={() => startBuildRef.current(
      Boolean(profile && response.status !== "fallback_cached"),
    )}>{startingBuild ? "Запускаем анализ…" : response.status === "not_found" || response.status === "fallback_cached" || response.status === "provider_not_configured"
      ? "Запустить технический анализ" : "Повторить технический анализ"}</button>}
    {response?.status === "rebuild_preserved" && <p>Новое подтверждение не получено; показан ранее сохранённый профиль.</p>}
    {(response?.status === "insufficient_evidence" || profile?.status === "insufficient_evidence") && <p>Недостаточно подтверждений для технических утверждений.</p>}
    {(response?.status === "no_sources" || response?.status === "insufficient_evidence" || profile?.status === "insufficient_evidence") && <GeneralChecklist />}
    {profile?.status === "complete" && <>
      <p>Технический профиль готов.</p>
      {(response?.knowledge_path ?? response?.diagnostics?.knowledge_path) === "local_corpus" && <p>Использованы ранее сохранённые технические источники.</p>}
      <p>{scopeLabels[response?.scope_level ?? "model"]} · {sourceCountLabel(profile.source_count)} · {confidenceLabels[profile.confidence]}</p>
      {profile.confidence === "limited" || profile.confidence === "low" ? <p>Доступны ограниченные данные: сведения могут относиться к модели в целом или основываться на отдельных сообщениях. Проверяйте их перед покупкой.</p> : null}
      <InspectionSections profile={profile} />
      {profile.risk_summary && <div className="knowledge-group"><h3>Итог для покупателя</h3>
        {profile.risk_summary_confidence && <small>{confidenceLabels[profile.risk_summary_confidence]} · {scopeLabels[profile.risk_summary_scope_label ?? "model"]}</small>}
        <p>{profile.risk_summary}</p></div>}
      {sources.length > 0 && <details><summary>Источники и подтверждения ({sources.length})</summary><ul>{sources.map((item) => (
        <li key={item.url}><a href={item.url} target="_blank" rel="noreferrer">{item.title || item.domain}</a> <small>{sourceTypeLabels[item.source_type] ?? "источник"} · {sourceStatusLabels[item.status] ?? "статус уточняется"}</small></li>
      ))}</ul></details>}
    </>}
  </section>;
}
