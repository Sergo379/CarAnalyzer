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
  year: "",
  body_type: "",
  transmission: "",
  region: "moscow_and_oblast",
  price: "",
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
    if (!form.brand.trim() || !form.model.trim() || form.year === "") return;
    const controller = new AbortController();
    const params = new URLSearchParams({
      brand: form.brand,
      model: form.model,
      year: String(form.year),
    });
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
  }, [form.brand, form.model, form.year]);

  useEffect(() => {
    setEngines([]);
    if (!form.brand.trim() || !form.model.trim() || form.year === "") return;
    const controller = new AbortController();
    const params = new URLSearchParams({
      brand: form.brand,
      model: form.model,
      year: String(form.year),
    });
    if (form.generation_id) params.set("generation_id", form.generation_id);
    fetch(`/api/catalog/engines?${params}`, { signal: controller.signal })
      .then((response) => response.ok ? response.json() : [])
      .then((items: CatalogOption[]) => setEngines(items))
      .catch(() => setEngines([]));
    return () => controller.abort();
  }, [form.brand, form.model, form.year, form.generation_id]);

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
    if (form.year === "") return setError("Напишите год выпуска автомобиля.");
    if (!form.body_type) return setError("Выберите тип кузова.");
    if (form.price === "") return setError("Напишите цену автомобиля.");
    setLoading(true);
    try {
      const response = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...form,
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
        <label>Год<input required placeholder="Написать год" type="number" min="1900" max={new Date().getFullYear() + 1} value={form.year} onChange={(e) => setForm({ ...form, year: e.target.value === "" ? "" : Number(e.target.value), generation_id: "", modification_id: "" })} /></label>
        <SearchableSelect label="Поколение" allowClear disabled={!form.brand.trim() || !form.model.trim() || form.year === ""} placeholder="Любое поколение" value={form.generation_id} options={generationOptions} onChange={(generation_id) => setForm({ ...form, generation_id, modification_id: "" })} />
        <SearchableSelect label="Двигатель" allowClear disabled={!form.brand.trim() || !form.model.trim() || form.year === ""} placeholder="Любой двигатель" value={form.modification_id} options={engineOptions} onChange={(modification_id) => setForm({ ...form, modification_id })} />
        <label>Кузов<select value={form.body_type} onChange={(e) => setForm({ ...form, body_type: e.target.value as BodyFilter | "" })}><option value="" disabled>Выбрать кузов</option>{Object.entries(bodyLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label>Коробка<select value={form.transmission} onChange={(e) => setForm({ ...form, transmission: e.target.value as Transmission | "" })}><option value="" disabled>Тип коробки</option>{Object.entries(transmissionLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <SearchableSelect label="Регион поиска" required value={form.region} options={regions} onChange={(region) => setForm({ ...form, region: region as SearchRegion })} />
        <label>Цена, ₽<input required placeholder="Написать цену" type="number" min="1" step="1" value={form.price} onChange={(e) => setForm({ ...form, price: e.target.value === "" ? "" : Number(e.target.value) })} /></label>
        <button disabled={loading}>{loading ? "Получаем свежие данные…" : "Найти конкурентов"}</button>
      </form>

      {error && <div className="alert error">{error}</div>}
      {result && (
        <>
          <section className="summary">
            <div><p className="eyebrow">Исходный автомобиль</p><h2>{result.source_vehicle.brand} {result.source_vehicle.model}, {result.source_vehicle.year}</h2><p>{bodyLabels[result.source_vehicle.body_type]} · {transmissionLabels[result.source_vehicle.transmission]} · {regions.find((region) => region.value === result.source_vehicle.region)?.label ?? result.source_vehicle.region} · {rubles.format(result.source_vehicle.price)}</p></div>
            <div className="statuses">{Object.entries(result.source_status).map(([source, status]) => <span title={result.source_details[source]} className={`status ${status}`} key={source}>{sourceLabels[source] ?? source} — {statusLabels[status] ?? status}</span>)}</div>
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
