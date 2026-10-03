import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const result = {
  source_vehicle: {
    brand: "Ford", model: "Fiesta", year_mode: "exact", year: 2016,
    year_from: null, year_to: null, body_type: "any", transmission: "any",
    region: "moscow_and_oblast", price_mode: "exact", price: 700000,
    price_from: null, price_to: null, modification: null, generation: null,
    segment: "passenger",
  },
  source_status: { "auto.ru": "partial", "drom.ru": "ok" },
  source_details: { "auto.ru": "discovery timeout", "drom.ru": "Работает" },
  source_operations: {
    "auto.ru": {
      target: { state: "ok", count: 1, detail: "Работает", elapsed_seconds: 1 },
      competitors: { state: "timeout", count: 0, detail: "timeout", elapsed_seconds: 30 },
    },
    "drom.ru": {
      target: { state: "empty", count: 0, detail: "Подходящих объявлений нет", elapsed_seconds: 1 },
      competitors: { state: "ok", count: 2, detail: "Работает", elapsed_seconds: 2 },
    },
  },
  source_listings: {
    listings: [{
      listing: {
        source: "auto.ru", external_id: "1", brand: "Ford", model: "Fiesta",
        modification: null, generation: null, fuel_type: null,
        engine_displacement: null, power_hp: null, engine_code: null, drivetrain: null,
        year: 2016, body_type: "hatchback", transmission: "automatic", price: 710000,
        url: "https://auto.ru/cars/used/sale/ford/fiesta/1-x/", location: "Москва",
        city: "Москва", region: "Москва", checked_at: "2026-01-01T00:00:00Z", segment: "passenger",
      },
      price_difference: 10000, price_difference_percent: 1.43,
    }],
    model_groups: [],
  },
  source_model_group: null,
  source_distribution: { "auto.ru": 1, "drom.ru": 0 },
  source_diagnostics: {}, source_operation_diagnostics: {}, pipeline_diagnostics: {},
  direct: { listings: [], model_groups: [] },
  expensive: { listings: [], model_groups: [] },
  cheaper: { listings: [], model_groups: [] },
  car_knowledge: {}, warnings: ["Auto.ru — источник работает частично"],
};

function response(data: unknown, ok = true) {
  return Promise.resolve({ ok, status: ok ? 200 : 500, json: async () => data });
}

let generationResponse: { status: string; generations: { id: string; label: string; name: string }[] };
let deferredKnowledge: Promise<unknown> | null;

function installFetch(searchResult: unknown = result) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = String(input);
    if (url.includes("/catalog/brands")) return response(["Ford", "BMW"]) as never;
    if (url.includes("/catalog/regions")) return response({ regions: [
      { value: "moscow_and_oblast", label: "Москва и Московская область" },
      { value: "tatarstan", label: "Республика Татарстан" },
    ] }) as never;
    if (url.includes("/catalog/models")) return response(["Fiesta", "Focus"]) as never;
    if (url.includes("/catalog/generation-state")) return response(generationResponse) as never;
    if (url.includes("/catalog/engines")) return response([
      { id: "engine:one", label: "1.6 л · бензин · 105 л.с.", name: "engine" },
    ]) as never;
    if (url.includes("/api/search") && init?.method === "POST") return response(searchResult) as never;
    if (url.includes("/api/knowledge/profile")) return (deferredKnowledge ?? response({ status: "provider_not_configured" })) as never;
    if (url.includes("/api/knowledge/sources")) return response({ sources: [] }) as never;
    return response([]) as never;
  });
}

beforeEach(() => {
  deferredKnowledge = null;
  generationResponse = { status: "ready", generations: [
    { id: "g1", label: "6 поколение", name: "6 поколение" },
    { id: "g2", label: "7 поколение", name: "7 поколение" },
  ] };
  installFetch();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("CarAnalyzer form", () => {
  it("uses dotted prices for target, averages, min/max, listings and RUB differences", async () => {
    vi.mocked(fetch).mockRestore();
    installFetch({ ...result, direct: {
      listings: result.source_listings.listings,
      model_groups: [{ brand: "Example Brand", model: "Example Model", listings_count: 2,
        average_price: 1000000, min_price: 900000, max_price: 1100000 }],
    } });
    render(<App />);
    fireEvent.change(screen.getByLabelText("Марка"), { target: { value: result.source_vehicle.brand } });
    fireEvent.change(screen.getByLabelText("Модель"), { target: { value: result.source_vehicle.model } });
    fireEvent.change(screen.getByLabelText("Год"), { target: { value: String(result.source_vehicle.year) } });
    fireEvent.change(screen.getByLabelText("Цена"), { target: { value: "700.000" } });
    fireEvent.click(screen.getByRole("button", { name: "Найти конкурентов" }));
    expect(await screen.findByText("1.000.000 ₽")).toBeInTheDocument();
    expect(screen.getByText("900.000 ₽ — 1.100.000 ₽")).toBeInTheDocument();
    expect(screen.getAllByText("710.000 ₽")).toHaveLength(2);
    expect(screen.getAllByText("+10.000 ₽")).toHaveLength(2);
    expect(screen.getByText(/700\.000 ₽/)).toBeInTheDocument();
    const payload = JSON.parse(String(vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes("/api/search"))?.[1]?.body));
    expect(payload.price).toBe(700000);
    expect(typeof payload.price).toBe("number");
  });

  it("submits numeric price ranges even when inputs contain separators", async () => {
    vi.mocked(fetch).mockRestore();
    installFetch({ ...result, source_vehicle: { ...result.source_vehicle,
      price_mode: "range", price: null, price_from: 1000000, price_to: 2000000 } });
    render(<App />);
    fireEvent.change(screen.getByLabelText("Марка"), { target: { value: result.source_vehicle.brand } });
    fireEvent.change(screen.getByLabelText("Модель"), { target: { value: result.source_vehicle.model } });
    fireEvent.change(screen.getByLabelText("Год"), { target: { value: String(result.source_vehicle.year) } });
    fireEvent.click(screen.getAllByRole("button", { name: "Диапазон" })[1]);
    fireEvent.change(screen.getByLabelText("Цена от"), { target: { value: "1.000.000" } });
    fireEvent.change(screen.getByLabelText("Цена до"), { target: { value: "2 000 000" } });
    fireEvent.click(screen.getByRole("button", { name: "Найти конкурентов" }));
    await screen.findByText("Рынок исходной модели");
    expect(screen.getByText(/1\.000\.000 ₽ — 2\.000\.000 ₽/)).toBeInTheDocument();
    const payload = JSON.parse(String(vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes("/api/search"))?.[1]?.body));
    expect(payload).toMatchObject({ price_mode: "range", price: null, price_from: 1000000, price_to: 2000000 });
  });
  it("shows an explicit no-generation status without selecting an option", async () => {
    generationResponse = { status: "source_has_no_generation_data", generations: [] };
    render(<App />);
    fireEvent.change(screen.getByLabelText("Марка"), { target: { value: "Ford" } });
    fireEvent.change(screen.getByLabelText("Модель"), { target: { value: "Fiesta" } });
    fireEvent.change(screen.getByLabelText("Год"), { target: { value: "2016" } });
    await waitFor(() => expect(screen.getByText("Источник подтвердил отсутствие данных о поколениях")).toBeInTheDocument());
    expect(screen.getByLabelText("Поколение")).toBeDisabled();
  });

  it("does not autoselect a generation from a parse error response", async () => {
    generationResponse = { status: "parse_error", generations: [
      { id: "g1", label: "6 поколение", name: "6 поколение" },
    ] };
    render(<App />);
    fireEvent.change(screen.getByLabelText("Марка"), { target: { value: "Ford" } });
    fireEvent.change(screen.getByLabelText("Модель"), { target: { value: "Fiesta" } });
    fireEvent.change(screen.getByLabelText("Год"), { target: { value: "2016" } });
    await waitFor(() => expect(screen.getByText("Формат данных о поколениях пока не распознан")).toBeInTheDocument());
    expect(screen.getByLabelText("Поколение")).toHaveValue("");
  });

  it("has dependent brand/model controls and explicit ANY filters", async () => {
    render(<App />);
    const [brand, model] = screen.getAllByRole("combobox");
    expect(model).toBeDisabled();
    fireEvent.change(brand, { target: { value: "Ford" } });
    expect(model).not.toBeDisabled();
    fireEvent.focus(model);
    await waitFor(() => expect(screen.getByRole("option", { name: "Fiesta" })).toBeInTheDocument());
    expect(screen.getByRole("option", { name: "Любой кузов" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Любая" })).toBeInTheDocument();
    expect(screen.getByLabelText("Кузов")).toHaveValue("any");
    expect(screen.getByLabelText("Коробка")).toHaveValue("any");
    expect(screen.queryByLabelText("Двигатель")).not.toBeInTheDocument();
    expect(screen.getByText("Точный год")).toBeInTheDocument();
    expect(screen.getAllByText("Диапазон")).toHaveLength(2);
  });

  it("resets generation when model/year dependencies change and hides engine input", async () => {
    render(<App />);
    const [brand, model] = screen.getAllByRole("combobox");
    fireEvent.change(brand, { target: { value: "Ford" } });
    fireEvent.change(model, { target: { value: "Fiesta" } });
    fireEvent.change(screen.getByLabelText("Год"), { target: { value: "2016" } });
    await waitFor(() => expect(screen.getByText("Поколения загружены")).toBeInTheDocument());
    const generation = screen.getByLabelText("Поколение");
    fireEvent.focus(generation);
    await waitFor(() => expect(screen.getByRole("option", { name: "6 поколение" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("option", { name: "6 поколение" }));
    expect(screen.queryByLabelText("Двигатель")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Модель"), { target: { value: "Focus" } });
    expect(screen.getByLabelText("Поколение")).toHaveValue("");
  });

  it("submits exact/range payload with region, ANY filters, and reference price", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<App />);
    fireEvent.change(screen.getByLabelText("Марка"), { target: { value: "Ford" } });
    fireEvent.change(screen.getByLabelText("Модель"), { target: { value: "Fiesta" } });
    fireEvent.change(screen.getByLabelText("Год"), { target: { value: "2016" } });
    fireEvent.focus(screen.getByLabelText("Регион поиска"));
    await waitFor(() => expect(screen.getByRole("option", { name: "Республика Татарстан" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("option", { name: "Республика Татарстан" }));
    fireEvent.change(screen.getByLabelText("Цена"), { target: { value: "700000" } });
    fireEvent.click(screen.getByRole("button", { name: "Найти конкурентов" }));
    await waitFor(() => expect(screen.getByText("Рынок исходной модели")).toBeInTheDocument());
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/search"));
    const payload = JSON.parse(String(call?.[1]?.body));
    expect(payload).toMatchObject({
      year_mode: "exact", year: 2016, body_type: "any", transmission: "any",
      region: "tatarstan", price_mode: "exact", price: 700000,
    });

    fireEvent.click(screen.getAllByRole("button", { name: "Диапазон" })[0]);
    expect(screen.getByLabelText("Год от")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Диапазон" })[1]);
    expect(screen.getByLabelText("Цена от")).toBeInTheDocument();
  });

  it("renders independent source states and safe marketplace links", async () => {
    render(<App />);
    fireEvent.change(screen.getByLabelText("Марка"), { target: { value: "Ford" } });
    fireEvent.change(screen.getByLabelText("Модель"), { target: { value: "Fiesta" } });
    fireEvent.change(screen.getByLabelText("Год"), { target: { value: "2016" } });
    fireEvent.change(screen.getByLabelText("Кузов"), { target: { value: "any" } });
    fireEvent.change(screen.getByLabelText("Цена"), { target: { value: "700000" } });
    fireEvent.click(screen.getByRole("button", { name: "Найти конкурентов" }));
    await waitFor(() => expect(screen.getByText(/рынок — работает · 1/)).toBeInTheDocument());
    expect(screen.getByText(/рынок — работает, результатов нет · 0/)).toBeInTheDocument();
    expect(screen.getByText(/конкуренты — превышен лимит времени · 0/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /Открыть/ });
    expect(link).toHaveAttribute("href", result.source_listings.listings[0].listing.url);
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("renders market results before the independent knowledge request finishes", async () => {
    let finishKnowledge: (value: unknown) => void = () => {};
    deferredKnowledge = new Promise((resolve) => { finishKnowledge = resolve; });
    render(<App />);
    fireEvent.change(screen.getByLabelText("Марка"), { target: { value: "Ford" } });
    fireEvent.change(screen.getByLabelText("Модель"), { target: { value: "Fiesta" } });
    fireEvent.change(screen.getByLabelText("Год"), { target: { value: "2016" } });
    fireEvent.change(screen.getByLabelText("Кузов"), { target: { value: "any" } });
    fireEvent.change(screen.getByLabelText("Цена"), { target: { value: "700000" } });
    fireEvent.click(screen.getByRole("button", { name: "Найти конкурентов" }));
    await waitFor(() => expect(screen.getByText("Рынок исходной модели")).toBeInTheDocument());
    expect(screen.getByText("Проверяем локальный профиль…")).toBeInTheDocument();
    finishKnowledge(await response({
      status: "cached", scope_key: "scope", scope_level: "model",
      profile: {
        status: "complete", common_problems: [], problematic_components: [],
        inspection_points: [{ title: "Проверка цепи", description: "Проверить шум при запуске",
          component: "engine", scope_key: "scope", evidence_refs: [1],
          support_source_count: 2, confidence: "medium" }],
        expensive_failures: [], risk_summary: "", confidence: "medium",
        source_count: 2, evidence_count: 1,
      },
    }));
    await waitFor(() => expect(screen.getByText("Проверка цепи")).toBeInTheDocument());
  });
});
