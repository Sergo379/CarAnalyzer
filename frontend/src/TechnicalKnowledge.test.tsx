import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TechnicalKnowledge } from "./TechnicalKnowledge";

const selection = {
  brand: "Example Brand",
  model: "Example Model",
  generation_id: "generation-id",
  engine_id: "",
};

function json(data: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: async () => data });
}

function claim(title: string, reference = 11) {
  return {
    title,
    description: `${title}: подтверждённое описание`,
    component: "engine",
    scope_key: "scope",
    evidence_refs: [reference],
    support_source_count: 1,
    confidence: "medium",
  };
}

function completeProfile(overrides: Record<string, unknown> = {}) {
  return {
    status: "complete",
    common_problems: [],
    problematic_components: [],
    inspection_points: [],
    expensive_failures: [],
    risk_summary: "",
    confidence: "medium",
    source_count: 2,
    evidence_count: 3,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("TechnicalKnowledge progress", () => {
  it("sends absent scope IDs as null so POST and omitted GET parameters agree", async () => {
    const mock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).includes("/profile")) return json({ status: "not_found" }) as never;
      return json({ status: "building", phase: "calling_gemini" }) as never;
    });
    render(<TechnicalKnowledge selection={{ ...selection, generation_id: "" }} />);
    await screen.findByText("Gemini анализирует технические данные");
    const build = mock.mock.calls.find(([url]) => String(url).endsWith("/build"));
    expect(JSON.parse(String(build?.[1]?.body))).toMatchObject({ generation_id: null, engine_id: null });
    expect(String(mock.mock.calls[0][0])).not.toContain("generation_id=");
  });
  it("shows real Gemini attempt, elapsed time and timeout without percentages", async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).includes("/profile")) return json({ status: "not_found" }) as never;
      return json({ status: "building", phase: "calling_gemini", provider: "gemini",
        provider_attempt: 1, provider_max_attempts: 3, provider_elapsed_seconds: 42,
        provider_timeout_seconds: 120, build_elapsed_seconds: 50 }) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByText("Gemini анализирует технические данные")).toBeInTheDocument();
    expect(screen.getByText(/Попытка 1 из 3 · прошло 42 сек · таймаут 120 сек/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByText(/прошло 43 сек/)).toBeInTheDocument();
    expect(screen.queryByText(/\d+%/)).not.toBeInTheDocument();
  });

  it("keeps polling after transient errors and retry phases, then fetches the final profile", async () => {
    vi.useFakeTimers();
    let polls = 0;
    let profiles = 0;
    const mock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/profile")) return json(++profiles === 1 ? { status: "not_found" } : {
        status: "cached", profile: completeProfile({ profile_version: 2,
          inspection_points: [claim("Финальный профиль")] }),
      }) as never;
      if (url.includes("/build")) return json({ status: "building", phase: "calling_gemini" }) as never;
      if (url.includes("/status")) {
        polls += 1;
        if (polls === 1) return Promise.reject(new Error("temporary network failure"));
        if (polls === 2) return json({ status: "provider_retry", phase: "provider_retry",
          provider_attempt: 2, provider_max_attempts: 3, retry_in_seconds: 4 }) as never;
        return json({ status: "complete" }) as never;
      }
      return Promise.reject(new Error("sources unavailable"));
    });
    render(<TechnicalKnowledge selection={selection} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(screen.getByText(/Повторная попытка через 4 сек · попытка 2 из 3/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(screen.getByText("Финальный профиль")).toBeInTheDocument();
    expect(profiles).toBe(2);
    expect(mock.mock.calls.filter(([url]) => String(url).includes("/build"))).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(polls).toBe(3);
  });
  it("groups supported high, medium and limited evidence without hiding partial sections", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/profile")) return json({
        status: "cached", scope_key: "scope", scope_level: "generation",
        profile: completeProfile({
          confidence: "limited",
          common_problems: [
            { ...claim("Сильное подтверждение"), confidence: "high", scope_label: "generation" },
            { ...claim("Среднее подтверждение", 14), confidence: "medium" },
            { ...claim("Ограниченное сообщение", 12), confidence: "limited", evidence_summary: "Сообщения владельцев" },
          ],
          inspection_points: [{ ...claim("Проверить узел", 13), confidence: "medium" }],
        }),
      }) as never;
      if (url.includes("/sources")) return json({ sources: [] }) as never;
      return json({}) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    expect(await screen.findByText("Сильное подтверждение")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Другие подтверждённые особенности" })).toBeInTheDocument();
    expect(screen.getByText("Ограниченное сообщение")).toBeInTheDocument();
    expect(screen.getByText("Проверить узел")).toBeInTheDocument();
    expect(screen.queryByText("Недостаточно подтверждений для технических утверждений.")).not.toBeInTheDocument();
  });

  it("renders a cached complete profile immediately with every populated section", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/profile")) {
        return json({
          status: "cached",
          scope_key: "scope",
          scope_level: "generation",
          profile: completeProfile({
            common_problems: [claim("Распространённая проблема")],
            problematic_components: [claim("Проблемный компонент", 12)],
            inspection_points: [claim("Пункт проверки", 13)],
            expensive_failures: [claim("Дорогая неисправность", 14)],
            risk_summary: "Итоговая оценка риска",
          }),
        }) as never;
      }
      if (url.includes("/sources")) return json({ sources: [] }) as never;
      return json({}) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    expect(await screen.findByText("Технический профиль готов.")).toBeInTheDocument();
    expect(screen.getByText("Распространённая проблема")).toBeInTheDocument();
    expect(screen.getByText("Проблемный компонент")).toBeInTheDocument();
    expect(screen.getByText("Пункт проверки")).toBeInTheDocument();
    expect(screen.getByText("Дорогая неисправность")).toBeInTheDocument();
    expect(screen.getByText("Итоговая оценка риска")).toBeInTheDocument();
    expect(screen.getByText(/2 независимых источника.*Средняя подтверждённость/)).toBeInTheDocument();
  });

  it("shows an active user-friendly build phase", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/profile")) return json({ status: "not_found" }) as never;
      if (url.includes("/build")) {
        return json({
          status: "building",
          phase: "loading_documents",
          scope_key: "scope",
          sources_discovered: 3,
        }) as never;
      }
      return new Promise(() => {}) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    expect(await screen.findByText("Собираем техническую информацию…")).toBeInTheDocument();
    expect(screen.getByText(/загрузка материалов/)).toBeInTheDocument();
    expect(screen.queryByText(/найдено источников: 3/)).not.toBeInTheDocument();
  });

  it("polls without restarting build and refreshes profile and sources", async () => {
    vi.useFakeTimers();
    let profileCalls = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/profile")) {
        profileCalls += 1;
        return json(profileCalls === 1 ? { status: "not_found" } : {
          status: "cached",
          scope_key: "scope",
          scope_level: "generation",
          profile: completeProfile({ inspection_points: [claim("Проверить компонент")] }),
        }) as never;
      }
      if (url.includes("/build")) {
        return json({ status: "building", phase: "discovering_sources" }) as never;
      }
      if (url.includes("/status")) {
        return json({
          status: "complete",
          phase: "complete",
          scope_key: "scope",
          scope_level: "generation",
        }) as never;
      }
      if (url.includes("/sources")) {
        return json({
          sources: [{
            url: "https://docs.example.org/item",
            title: "Technical source",
            domain: "docs.example.org",
            source_type: "technical_article",
            status: "complete",
            chunk_ids: [11],
          }],
        }) as never;
      }
      return json({}) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByText("Проверить компонент")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Technical source" })).toBeInTheDocument();
    expect(profileCalls).toBe(2);
    const buildCalls = fetchMock.mock.calls.filter(([url]) => String(url).includes("/build"));
    expect(buildCalls).toHaveLength(1);
  });

  it("shows a legacy cached profile immediately while upgrading from the local corpus", async () => {
    vi.useFakeTimers();
    let profileCalls = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/profile")) {
        profileCalls += 1;
        return json({ status: "cached", scope_key: "scope", profile: profileCalls === 1
          ? completeProfile({ profile_version: 1, inspection_points: [claim("Старое описание")] })
          : completeProfile({ profile_version: 2, inspection_points: [{
            ...claim("Проверка при выкупе"), inspection_category: "body",
            what_to_check: ["Осмотреть кромку двери"],
          }] }),
        }) as never;
      }
      if (url.includes("/rebuild")) return json({ status: "building", phase: "retrieving" }) as never;
      if (url.includes("/status")) return json({ status: "complete", scope_key: "scope" }) as never;
      return json({ sources: [] }) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByText("Старое описание")).toBeInTheDocument();
    expect(screen.getByText("Собираем техническую информацию…")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(screen.getByText("Проверка при выкупе")).toBeInTheDocument();
    expect(screen.getByText("Осмотреть кромку двери")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/rebuild"))).toHaveLength(1);
  });

  it("renders available fields when optional profile sections are absent", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/profile")) {
        return json({
          status: "cached",
          scope_key: "scope",
          profile: completeProfile({
            common_problems: undefined,
            problematic_components: undefined,
            inspection_points: [claim("Доступный пункт проверки")],
            expensive_failures: undefined,
            risk_summary: "Доступная сводка",
          }),
        }) as never;
      }
      if (url.includes("/sources")) return json({ sources: [] }) as never;
      return json({}) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    expect(await screen.findByText("Доступный пункт проверки")).toBeInTheDocument();
    expect(screen.getByText("Доступная сводка")).toBeInTheDocument();
  });

  it("does not let a late response from the previous selection overwrite the current one", async () => {
    let resolveOld: ((value: unknown) => void) | undefined;
    const oldProfile = new Promise((resolve) => { resolveOld = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("model=Old+Model") && url.includes("/profile")) {
        return oldProfile as never;
      }
      if (url.includes("model=New+Model") && url.includes("/profile")) {
        return json({
          status: "cached",
          scope_key: "new-scope",
          profile: completeProfile({ inspection_points: [claim("Новый профиль")] }),
        }) as never;
      }
      if (url.includes("/sources")) return json({ sources: [] }) as never;
      return json({}) as never;
    });
    const view = render(<TechnicalKnowledge selection={{ ...selection, model: "Old Model" }} />);
    view.rerender(<TechnicalKnowledge selection={{ ...selection, model: "New Model" }} />);
    expect(await screen.findByText("Новый профиль")).toBeInTheDocument();
    await act(async () => {
      resolveOld?.(await json({
        status: "cached",
        scope_key: "old-scope",
        profile: completeProfile({ inspection_points: [claim("Старый профиль")] }),
      }));
      await Promise.resolve();
    });
    expect(screen.queryByText("Старый профиль")).not.toBeInTheDocument();
    expect(screen.getByText("Новый профиль")).toBeInTheDocument();
  });

  it("shows distinct failed and insufficient-evidence terminal states", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockImplementation((input) => {
      if (String(input).includes("/profile")) return json({ status: "failed" }) as never;
      return json({ sources: [] }) as never;
    });
    const view = render(<TechnicalKnowledge selection={selection} />);
    expect(await screen.findByText(/Сбор знаний завершился ошибкой/)).toBeInTheDocument();

    fetchMock.mockImplementation((input) => {
      if (String(input).includes("/profile")) {
        return json({
          status: "insufficient_evidence",
          scope_key: "other-scope",
          profile: { ...completeProfile(), status: "insufficient_evidence" },
        }) as never;
      }
      return json({ sources: [] }) as never;
    });
    view.rerender(<TechnicalKnowledge selection={{ ...selection, generation_id: "other" }} />);
    expect(await screen.findByText(/Недостаточно подтверждений/)).toBeInTheDocument();
  });

  it("shows cached fallback while starting an exact-scope build", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/profile")) {
        return json({
          status: "fallback_cached",
          phase: "fallback_cached",
          scope_key: "model-scope",
          scope_level: "model",
          profile: {
            status: "complete",
            common_problems: [],
            problematic_components: [],
            inspection_points: [{
              title: "Общая проверка",
              description: "Общая проверка состояния перед покупкой",
              component: "",
              scope_key: "model-scope",
              evidence_refs: [7],
              support_source_count: 1,
              confidence: "low",
            }],
            expensive_failures: [],
            risk_summary: "",
            confidence: "low",
            source_count: 1,
            evidence_count: 1,
          },
        }) as never;
      }
      if (url.includes("/build")) {
        return json({ status: "building", phase: "discovering_sources" }) as never;
      }
      if (url.includes("/sources")) return json({ sources: [] }) as never;
      return new Promise(() => {}) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    expect(await screen.findByText("Общая проверка")).toBeInTheDocument();
    expect(screen.getByText("Собираем техническую информацию…")).toBeInTheDocument();
    const buildCalls = fetchMock.mock.calls.filter(([url]) => String(url).includes("/build"));
    expect(buildCalls).toHaveLength(1);
  });

  it("separates service-only checks and hides internal evidence IDs", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/profile")) return json({
        status: "cached", scope_key: "scope",
        profile: completeProfile({
          inspection_points: [{ ...claim("Проверка на тест-драйве", 654),
            buyer_checks: ["Прослушать шум на коротком тест-драйве"],
            inspection_methods: ["sound", "test_drive"],
            warning_signs: ["Посторонний шум"],
          }],
          expensive_failures: [{ ...claim("Внутренний износ", 655),
            requires_service: true, service_note: "Нужна диагностика в сервисе",
            inspection_methods: ["service_required"],
          }],
        }),
      }) as never;
      if (url.includes("/sources")) return json({ sources: [] }) as never;
      return json({}) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    expect(await screen.findByText("Проверка на тест-драйве")).toBeInTheDocument();
    expect(screen.getByText("Прослушать шум на коротком тест-драйве")).toBeInTheDocument();
    expect(screen.getByText(/Метод: звук, тест-драйв/)).toBeInTheDocument();
    expect(screen.getByText("Что проверить на сервисе")).toBeInTheDocument();
    expect(screen.queryByText(/фрагменты: 654|фрагменты: 655/)).not.toBeInTheDocument();
  });

  it("shows only a generic checklist when model-specific evidence is absent", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).includes("/profile")) return json({
        status: "insufficient_evidence", scope_key: "scope",
        profile: { ...completeProfile(), status: "insufficient_evidence" },
      }) as never;
      return json({ sources: [] }) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    expect(await screen.findByText("Базовая проверка при выкупе")).toBeInTheDocument();
    expect(screen.getByText(/не относится к подтверждённым особенностям выбранной модели/)).toBeInTheDocument();
  });

  it("distinguishes a temporary Gemini outage from missing evidence", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).includes("/profile")) return json({
        status: "provider_temporarily_unavailable", scope_key: "scope",
      }) as never;
      return json({ sources: [] }) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    expect(await screen.findByText(/Gemini временно недоступен/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить сбор знаний" })).toBeInTheDocument();
    expect(screen.queryByText("Базовая проверка при выкупе")).not.toBeInTheDocument();
  });

  it("groups actionable buyout items and service history without RAG internals", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).includes("/profile")) return json({
        status: "cached", scope_key: "scope", profile: completeProfile({
          inspection_points: [
            { ...claim("Уплотнения дверей"), inspection_category: "body", system_name: "Door seals",
              what_to_check: ["Осмотреть нижнюю кромку двери"],
              warning_signs: ["Следы влаги"], how_to_check: ["Осмотреть при дневном свете"],
              inspection_methods: ["visual"],
              service_history_checks: ["Сверить записи о замене уплотнений"] },
            { ...claim("Шум при запуске", 12), inspection_category: "engine",
              what_to_check: ["Послушать запуск холодного двигателя"],
              inspection_methods: ["cold_start"] },
          ],
        }),
      }) as never;
      return json({ sources: [] }) as never;
    });
    render(<TechnicalKnowledge selection={selection} />);
    expect(await screen.findByRole("heading", { name: "Кузов и состояние" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Двигатель и холодный запуск" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Что проверить по истории обслуживания" })).toBeInTheDocument();
    expect(screen.getByText("Сверить записи о замене уплотнений")).toBeInTheDocument();
    expect(screen.getByText(/Метод: холодный запуск/)).toBeInTheDocument();
    expect(screen.queryByText(/chunk_id|evidence_refs|reranker|654/)).not.toBeInTheDocument();
  });
});
