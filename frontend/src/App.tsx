import { type FormEvent, useEffect, useMemo, useState } from "react";

import { SearchableSelect, type SelectOption } from "./SearchableSelect";

import type {
  BodyFilter,
  CategoryResult,
  ClassifiedListing,
  SearchForm,
  SearchResult,
  SearchRegion,
  Transmission,
} from "./types";

const bodyLabels: Record<BodyFilter, string> = {
  any: "Любой кузов",
  sedan: "Седан",
  wagon: "Универсал",
  hatchback: "Хэтчбек",
  liftback: "Лифтбек",
  coupe: "Купе",
  convertible: "Кабриолет",
  suv: "Внедорожник",
  crossover: "Кроссовер",
  pickup: "Пикап",
  minivan: "Минивэн",
  van: "Фургон",
};

const initialForm: SearchForm = {
  brand: "",
  model: "",
  year_mode: "exact",
  year: "",
  year_from: "",
  year_to: "",
  body_type: "",
  transmission: "",
  region: "moscow_and_oblast",
  price_mode: "exact",
  price: "",
  price_from: "",
  price_to: "",
  generation_id: "",
  modification_id: "",
};

interface CatalogOption {
  id: string;
  label: string;
  name: string;
}

const transmissionLabels: Record<Transmission, string> = {
  any: "Любая",
  automatic: "Автомат",
  manual: "Механика",
  robot: "Робот",
  cvt: "Вариатор",
};

const fallbackRegions: SelectOption[] = [
  { value: "any", label: "Любой регион" },
  { value: "moscow", label: "Москва" },
  { value: "moscow_oblast", label: "Московская область" },
  { value: "moscow_and_oblast", label: "Москва и Московская область" },
];

const statusLabels: Record<string, string> = {
  ok: "работает",
  partial: "работает частично",
  empty: "работает, результатов нет",
  captcha_required: "требуется ручная проверка",
  auth_required: "требуется авторизация",
  http_429: "ограничен автоматический HTTP-доступ",
  http_403: "доступ отклонён площадкой",
  http_automation_limited: "ограничен автоматический HTTP-доступ",
  browser_access_limited: "ограничен доступ из изолированного браузера",
  parse_error: "формат страницы не распознан",
  unsupported_query: "запрос не поддерживается",
  robots_restricted: "ограничен правилами площадки",
  blocked: "источник ограничил доступ",
  timeout: "превышен лимит времени",
  error: "временно недоступен",
};

const sourceLabels: Record<string, string> = {
  "auto.ru": "Auto.ru",
  avito: "Avito",
  "drom.ru": "Drom",
};

const rubles = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});
const checkedAt = new Intl.DateTimeFormat("ru-RU", {
  dateStyle: "short",
  timeStyle: "short",
});

function ListingRow({ item }: { item: ClassifiedListing }) {
  const differenceClass = item.price_difference > 0 ? "positive" : "negative";
  return (
    <tr>
      <td>
        <strong>{item.listing.brand} {item.listing.model}</strong>
        {item.listing.modification && <span className="muted">{item.listing.modification}</span>}
        <span className="muted">{item.listing.location ?? "Регион не указан"}</span>
      </td>
      <td>{item.listing.year}</td>
      <td>{bodyLabels[item.listing.body_type]}</td>
      <td>{item.listing.transmission ? transmissionLabels[item.listing.transmission] : "—"}</td>
      <td className="price">{rubles.format(item.listing.price)}</td>
      <td className={differenceClass}>
        {item.price_difference > 0 ? "+" : ""}{rubles.format(item.price_difference)}
        <span>{item.price_difference > 0 ? "+" : ""}{item.price_difference_percent}%</span>
      </td>
      <td><span className="source">{item.listing.source}</span></td>
      <td>{checkedAt.format(new Date(item.listing.checked_at))}</td>
      <td><a href={item.listing.url} target="_blank" rel="noreferrer">Открыть ↗</a></td>
    </tr>
  );
}

function Category({ title, subtitle, data }: { title: string; subtitle: string; data: CategoryResult }) {
  return (
    <section className="result-section">
      <div className="section-heading">
        <div><p className="eyebrow">{subtitle}</p><h2>{title}</h2></div>
        <span className="count">{data.listings.length}</span>
      </div>
      {data.model_groups.length > 0 && (
        <div className="model-grid">
          {data.model_groups.map((group) => (
            <article className="model-card" key={`${group.brand}-${group.model}`}>
              <h3>{group.brand} {group.model}</h3>
              <p>{group.listings_count} объявлений</p>
              <dl>
                <div><dt>Средняя</dt><dd>{rubles.format(group.average_price)}</dd></div>
                <div><dt>Диапазон</dt><dd>{rubles.format(group.min_price)} — {rubles.format(group.max_price)}</dd></div>
              </dl>
            </article>
          ))}
        </div>
      )}
      {data.listings.length === 0 ? (
        <p className="empty">Подходящих объявлений в свежей выдаче нет.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Автомобиль</th><th>Год</th><th>Кузов</th><th>Коробка</th><th>Цена</th><th>Разница</th><th>Источник</th><th>Проверено</th><th></th></tr></thead>
            <tbody>{data.listings.map((item) => <ListingRow key={`${item.listing.source}-${item.listing.external_id}`} item={item} />)}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function App() {
  const [form, setForm] = useState<SearchForm>(initialForm);
  const [result, setResult] = useState<SearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [brands, setBrands] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [generations, setGenerations] = useState<CatalogOption[]>([]);
  const [engines, setEngines] = useState<CatalogOption[]>([]);
  const [regions, setRegions] = useState<SelectOption[]>(fallbackRegions);
  const yearReady = form.year_mode === "exact"
    ? form.year !== ""
    : form.year_from !== "" && form.year_to !== "" && form.year_from <= form.year_to;

  useEffect(() => {
    Promise.all([
      fetch("/api/catalog/brands").then((response) => response.json()),
      fetch("/api/catalog/regions").then((response) => response.json()),
    ]).then(([brandData, regionData]) => {
      setBrands(brandData as string[]);
      setRegions(regionData.regions as SelectOption[]);
    }).catch(() => {
      // Generic manual brand/model entry remains available if reference data is offline.
    });
  }, []);

  useEffect(() => {
    setModels([]);
    const brand = form.brand.trim();
    if (!brand) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      fetch(`/api/catalog/models?brand=${encodeURIComponent(brand)}`, {
        signal: controller.signal,
      })
        .then((response) => response.ok ? response.json() : [])
        .then((modelData) => setModels(modelData as string[]))
        .catch((reason: unknown) => {
          if (!(reason instanceof DOMException && reason.name === "AbortError")) setModels([]);
        });
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [form.brand]);

  useEffect(() => {
    setGenerations([]);
    setEngines([]);
    if (!form.brand.trim() || !form.model.trim() || !yearReady) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ brand: form.brand, model: form.model });
    if (form.year_mode === "exact") params.set("year", String(form.year));
    else {
      params.set("year_from", String(form.year_from));
      params.set("year_to", String(form.year_to));
    }
    fetch(`/api/catalog/generations?${params}`, { signal: controller.signal })
      .then((response) => response.ok ? response.json() : [])
      .then((items: CatalogOption[]) => {
        setGenerations(items);
        if (items.length === 1) {
          setForm((current) => ({ ...current, generation_id: items[0].id }));
        }
      })
      .catch(() => setGenerations([]));
    return () => controller.abort();
  }, [form.brand, form.model, form.year_mode, form.year, form.year_from, form.year_to, yearReady]);

  useEffect(() => {
    setEngines([]);
    if (!form.brand.trim() || !form.model.trim() || !yearReady) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ brand: form.brand, model: form.model });
    if (form.year_mode === "exact") params.set("year", String(form.year));
    else {
      params.set("year_from", String(form.year_from));
      params.set("year_to", String(form.year_to));
    }
    if (form.generation_id) params.set("generation_id", form.generation_id);
    fetch(`/api/catalog/engines?${params}`, { signal: controller.signal })
      .then((response) => response.ok ? response.json() : [])
      .then((items: CatalogOption[]) => setEngines(items))
      .catch(() => setEngines([]));
    return () => controller.abort();
  }, [form.brand, form.model, form.year_mode, form.year, form.year_from, form.year_to, form.generation_id, yearReady]);

  const brandOptions = useMemo(
    () => brands.map((brand) => ({ value: brand, label: brand })),
    [brands],
  );
  const modelOptions = useMemo(
    () => models.map((model) => ({ value: model, label: model })),
    [models],
  );
  const generationOptions = useMemo(
    () => generations.map((item) => ({ value: item.id, label: item.label })),
    [generations],
  );
  const engineOptions = useMemo(
    () => engines.map((item) => ({ value: item.id, label: item.label })),
    [engines],
  );

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!form.brand.trim()) return setError("Выберите или напишите марку автомобиля.");
    if (!form.model.trim()) return setError("Выберите или напишите модель автомобиля.");
    if (!yearReady) return setError("Укажите корректный год или диапазон годов.");
    if (!form.body_type) return setError("Выберите тип кузова.");
    const priceReady = form.price_mode === "exact"
      ? form.price !== ""
      : form.price_from !== "" && form.price_to !== "" && form.price_from <= form.price_to;
    if (!priceReady) return setError("Укажите корректную цену или диапазон цен.");
    setLoading(true);
    try {
      const response = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...form,
          year: form.year_mode === "exact" ? form.year : null,
          year_from: form.year_mode === "range" ? form.year_from : null,
          year_to: form.year_mode === "range" ? form.year_to : null,
          price: form.price_mode === "exact" ? form.price : null,
          price_from: form.price_mode === "range" ? form.price_from : null,
          price_to: form.price_mode === "range" ? form.price_to : null,
          transmission: form.transmission || "any",
        }),
      });
      if (!response.ok) {
        if (response.status === 502) {
          throw new Error("Backend недоступен: запустите CarAnalyzer server на 127.0.0.1:8000");
        }
        throw new Error(`Backend вернул HTTP ${response.status}`);
      }
      setResult(await response.json() as SearchResult);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось выполнить поиск");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main>
      <header className="hero">
        <div className="brand-mark">CA</div>
        <div><p className="eyebrow">Локальная рыночная аналитика</p><h1>CarAnalyzer</h1><p>Свежие объявления и честное сравнение автомобилей по цене, кузову и сегменту.</p></div>
      </header>

      <form className="search-card" onSubmit={submit} noValidate>
        <SearchableSelect label="Марка" required allowCustom placeholder="Выбрать марку" value={form.brand} options={brandOptions} onChange={(brand) => {
          setForm({
            ...form,
            brand,
            model: brand === form.brand ? form.model : "",
            generation_id: "",
            modification_id: "",
          });
        }} />
        <SearchableSelect label="Модель" required allowCustom disabled={!form.brand.trim()} placeholder="Выбрать модель" value={form.model} options={modelOptions} onChange={(model) => setForm({ ...form, model, generation_id: "", modification_id: "" })} />
        <div className="range-field">
          <span>Год</span>
          <div className="mode-toggle">
            <button type="button" className={form.year_mode === "exact" ? "active" : ""} onClick={() => setForm({ ...form, year_mode: "exact", generation_id: "", modification_id: "" })}>Точный год</button>
            <button type="button" className={form.year_mode === "range" ? "active" : ""} onClick={() => setForm({ ...form, year_mode: "range", generation_id: "", modification_id: "" })}>Диапазон</button>
          </div>
          {form.year_mode === "exact" ? (
            <input required aria-label="Год" placeholder="Написать год" type="number" min="1900" max={new Date().getFullYear() + 1} value={form.year} onChange={(e) => setForm({ ...form, year: e.target.value === "" ? "" : Number(e.target.value), generation_id: "", modification_id: "" })} />
          ) : (
            <div className="range-inputs">
              <input required aria-label="Год от" placeholder="Год от" type="number" min="1900" value={form.year_from} onChange={(e) => setForm({ ...form, year_from: e.target.value === "" ? "" : Number(e.target.value), generation_id: "", modification_id: "" })} />
              <input required aria-label="Год до" placeholder="Год до" type="number" min="1900" max={new Date().getFullYear() + 1} value={form.year_to} onChange={(e) => setForm({ ...form, year_to: e.target.value === "" ? "" : Number(e.target.value), generation_id: "", modification_id: "" })} />
            </div>
          )}
        </div>
        <SearchableSelect label="Поколение" allowClear disabled={!form.brand.trim() || !form.model.trim() || !yearReady} placeholder={form.year_mode === "range" ? "Все поколения в диапазоне" : "Любое поколение"} value={form.generation_id} options={generationOptions} onChange={(generation_id) => setForm({ ...form, generation_id, modification_id: "" })} />
        <SearchableSelect label="Двигатель" allowClear disabled={!form.brand.trim() || !form.model.trim() || !yearReady} placeholder="Любой двигатель" value={form.modification_id} options={engineOptions} onChange={(modification_id) => setForm({ ...form, modification_id })} />
        <label>Кузов<select value={form.body_type} onChange={(e) => setForm({ ...form, body_type: e.target.value as BodyFilter | "" })}><option value="" disabled>Выбрать кузов</option>{Object.entries(bodyLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label>Коробка<select value={form.transmission} onChange={(e) => setForm({ ...form, transmission: e.target.value as Transmission | "" })}><option value="" disabled>Тип коробки</option>{Object.entries(transmissionLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <SearchableSelect label="Регион поиска" required value={form.region} options={regions} onChange={(region) => setForm({ ...form, region: region as SearchRegion })} />
        <div className="range-field">
          <span>Цена, ₽</span>
          <div className="mode-toggle">
            <button type="button" className={form.price_mode === "exact" ? "active" : ""} onClick={() => setForm({ ...form, price_mode: "exact" })}>Точная цена</button>
            <button type="button" className={form.price_mode === "range" ? "active" : ""} onClick={() => setForm({ ...form, price_mode: "range" })}>Диапазон</button>
          </div>
          {form.price_mode === "exact" ? (
            <input required aria-label="Цена" placeholder="Написать цену" type="number" min="1" step="1" value={form.price} onChange={(e) => setForm({ ...form, price: e.target.value === "" ? "" : Number(e.target.value) })} />
          ) : (
            <div className="range-inputs">
              <input required aria-label="Цена от" placeholder="Цена от" type="number" min="1" step="1" value={form.price_from} onChange={(e) => setForm({ ...form, price_from: e.target.value === "" ? "" : Number(e.target.value) })} />
              <input required aria-label="Цена до" placeholder="Цена до" type="number" min="1" step="1" value={form.price_to} onChange={(e) => setForm({ ...form, price_to: e.target.value === "" ? "" : Number(e.target.value) })} />
            </div>
          )}
        </div>
        <button disabled={loading}>{loading ? "Получаем свежие данные…" : "Найти конкурентов"}</button>
      </form>

      {error && <div className="alert error">{error}</div>}
      {result && (
        <>
          <section className="summary">
            <div><p className="eyebrow">Исходный автомобиль</p><h2>{result.source_vehicle.brand} {result.source_vehicle.model}, {result.source_vehicle.year_mode === "exact" ? result.source_vehicle.year : `${result.source_vehicle.year_from}–${result.source_vehicle.year_to}`}</h2><p>{bodyLabels[result.source_vehicle.body_type]} · {transmissionLabels[result.source_vehicle.transmission]} · {regions.find((region) => region.value === result.source_vehicle.region)?.label ?? result.source_vehicle.region} · {result.source_vehicle.price_mode === "exact" ? rubles.format(result.source_vehicle.price ?? 0) : `${rubles.format(result.source_vehicle.price_from ?? 0)} — ${rubles.format(result.source_vehicle.price_to ?? 0)}`}</p></div>
            <div className="statuses">{Object.entries(result.source_operations).map(([source, operations]) => <span title={`${operations.target.detail}; ${operations.competitors.detail}`} className={`status ${result.source_status[source]}`} key={source}><strong>{sourceLabels[source] ?? source}</strong><br />рынок — {statusLabels[operations.target.state] ?? operations.target.state} · {operations.target.count}<br />конкуренты — {statusLabels[operations.competitors.state] ?? operations.competitors.state} · {operations.competitors.count}</span>)}</div>
          </section>
          {result.warnings.length > 0 && <div className="alert warning"><strong>Часть источников недоступна</strong>{result.warnings.map((warning) => <span key={warning}>{warning}</span>)}</div>}
          <Category title="Рынок исходной модели" subtitle="Объявления исходной модели" data={result.source_listings} />
          <Category title="Прямые конкуренты" subtitle="Рядом по цене" data={result.direct} />
          <Category title="Более дорогие" subtitle="До +20%" data={result.expensive} />
          <Category title="Более дешёвые" subtitle="На 10–20% ниже" data={result.cheaper} />
          <section className="knowledge">
            <p className="eyebrow">Знания по моделям результата</p>
            {Object.entries(result.car_knowledge).map(([key, item]) => (
              <div key={key}>
                <h2>{item.profile?.brand} {item.profile?.model}</h2>
                {item.status === "ai_provider_not_configured" ? <p>Профиль создан. AI не обязателен для поиска; неподтверждённые проблемы не показываются.</p> : <p>Статус: {item.status}</p>}
              </div>
            ))}
          </section>
        </>
      )}
    </main>
  );
}
