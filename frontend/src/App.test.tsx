import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
      target: { state: "ok", count: 1, detail: "Работает", elapsed_seconds: 1 },
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
  source_distribution: { "auto.ru": 1, "drom.ru": 1 },
  source_diagnostics: {}, source_operation_diagnostics: {}, pipeline_diagnostics: {},
  direct: { listings: [], model_groups: [] },
  expensive: { listings: [], model_groups: [] },
  cheaper: { listings: [], model_groups: [] },
  car_knowledge: {}, warnings: ["Auto.ru — источник работает частично"],
};

function response(data: unknown, ok = true) {
  return Promise.resolve({ ok, status: ok ? 200 : 500, json: async () => data });
}

function installFetch(searchResult: unknown = result) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = String(input);
    if (url.includes("/catalog/brands")) return response(["Ford", "BMW"]) as never;
    if (url.includes("/catalog/regions")) return response({ regions: [
      { value: "moscow_and_oblast", label: "Москва и Московская область" },
      { value: "tatarstan", label: "Республика Татарстан" },
    ] }) as never;
    if (url.includes("/catalog/models")) return response(["Fiesta", "Focus"]) as never;
    if (url.includes("/catalog/generations")) return response([
      { id: "g1", label: "6 поколение", name: "6 поколение" },
      { id: "g2", label: "7 поколение", name: "7 поколение" },
    ]) as never;
    if (url.includes("/catalog/engines")) return response([
      { id: "engine:one", label: "1.6 л · бензин · 105 л.с.", name: "engine" },
    ]) as never;
    if (url.includes("/api/search") && init?.method === "POST") return response(searchResult) as never;
    return response([]) as never;
  });
}

beforeEach(() => installFetch());
afterEach(() => vi.restoreAllMocks());

describe("CarAnalyzer form", () => {
  it("has dependent brand/model controls and explicit ANY filters", async () => {
    render(<App />);
    const [brand, model] = screen.getAllByRole("combobox");
    expect(model).toBeDisabled();
    fireEvent.change(brand, { target: { value: "Ford" } });
    expect(model).not.toBeDisabled();
    await waitFor(() => expect(screen.getByRole("option", { name: "Fiesta" })).toBeInTheDocument());
    expect(screen.getByRole("option", { name: "Любой кузов" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Любая" })).toBeInTheDocument();
    expect(screen.getByText("Точный год")).toBeInTheDocument();
    expect(screen.getByText("Диапазон")).toBeInTheDocument();
  });

  it("resets generation and engine when model/year/generation dependencies change", async () => {
    render(<App />);
    const [brand, model] = screen.getAllByRole("combobox");
    fireEvent.change(brand, { target: { value: "Ford" } });
    fireEvent.change(model, { target: { value: "Fiesta" } });
    fireEvent.change(screen.getByLabelText("Год"), { target: { value: "2016" } });
    await waitFor(() => expect(screen.getByRole("option", { name: "6 поколение" })).toBeInTheDocument());
    const generation = screen.getByLabelText("Поколение");
    fireEvent.change(generation, { target: { value: "g1" } });
    await waitFor(() => expect(screen.getByRole("option", { name: /1.6 л/ })).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Модель"), { target: { value: "Focus" } });
    expect(screen.getByLabelText("Поколение")).toHaveValue("");
    expect(screen.getByLabelText("Двигатель")).toHaveValue("");
  });

  it("submits exact/range payload with region, ANY filters, and reference price", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<App />);
    fireEvent.change(screen.getByLabelText("Марка"), { target: { value: "Ford" } });
    fireEvent.change(screen.getByLabelText("Модель"), { target: { value: "Fiesta" } });
    fireEvent.change(screen.getByLabelText("Год"), { target: { value: "2016" } });
    fireEvent.change(screen.getByLabelText("Кузов"), { target: { value: "any" } });
    fireEvent.change(screen.getByLabelText("Коробка"), { target: { value: "any" } });
    fireEvent.change(screen.getByLabelText("Регион поиска"), { target: { value: "tatarstan" } });
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
    expect(screen.getByText(/конкуренты — превышен лимит времени · 0/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /Открыть/ });
    expect(link).toHaveAttribute("href", result.source_listings.listings[0].listing.url);
    expect(link).toHaveAttribute("rel", "noreferrer");
  });
});
