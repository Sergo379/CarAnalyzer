import { type FormEvent, useEffect, useMemo, useState } from "react";

import { SearchableSelect, type SelectOption } from "./SearchableSelect";

import type {
  BodyType,
  CategoryResult,
  ClassifiedListing,
  SearchForm,
  SearchResult,
  SearchRegion,
  Transmission,
} from "./types";

const bodyLabels: Record<BodyType, string> = {
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
  brand: "BMW",
  model: "520i",
  year: 2022,
  body_type: "sedan",
  transmission: "any",
  region: "moscow_and_oblast",
  price: 4_100_000,
};

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
  http_429: "временно недоступен (ограничение IP)",
  http_403: "доступ отклонён площадкой",
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
  const [vehicleCatalog, setVehicleCatalog] = useState<Record<string, string[]>>({});
  const [regions, setRegions] = useState<SelectOption[]>(fallbackRegions);

  useEffect(() => {
    Promise.all([
      fetch("/api/catalog/vehicles").then((response) => response.json()),
      fetch("/api/catalog/regions").then((response) => response.json()),
    ]).then(([vehicles, regionData]) => {
      setVehicleCatalog(Object.fromEntries(
        (vehicles.brands as { name: string; models: string[] }[])
          .map((entry) => [entry.name, entry.models]),
      ));
      setRegions(regionData.regions as SelectOption[]);
    }).catch(() => {
      // Generic manual brand/model entry remains available if reference data is offline.
    });
  }, []);

  const brandOptions = useMemo(
    () => Object.keys(vehicleCatalog).map((brand) => ({ value: brand, label: brand })),
    [vehicleCatalog],
  );
  const modelOptions = useMemo(
    () => (vehicleCatalog[form.brand] ?? []).map((model) => ({ value: model, label: model })),
    [vehicleCatalog, form.brand],
  );

  async function submit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const response = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
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

      <form className="search-card" onSubmit={submit}>
        <SearchableSelect label="Марка" required allowCustom value={form.brand} options={brandOptions} onChange={(brand) => {
          const compatible = vehicleCatalog[brand] ?? [];
          setForm({ ...form, brand, model: compatible.includes(form.model) ? form.model : "" });
        }} />
        <SearchableSelect label="Модель" required allowCustom value={form.model} options={modelOptions} onChange={(model) => setForm({ ...form, model })} />
        <label>Год<input required type="number" min="1900" max={new Date().getFullYear() + 1} value={form.year} onChange={(e) => setForm({ ...form, year: Number(e.target.value) })} /></label>
        <label>Кузов<select value={form.body_type} onChange={(e) => setForm({ ...form, body_type: e.target.value as BodyType })}>{Object.entries(bodyLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label>Коробка<select value={form.transmission} onChange={(e) => setForm({ ...form, transmission: e.target.value as Transmission })}>{Object.entries(transmissionLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <SearchableSelect label="Регион поиска" required value={form.region} options={regions} onChange={(region) => setForm({ ...form, region: region as SearchRegion })} />
        <label>Цена, ₽<input required type="number" min="1" step="1" value={form.price} onChange={(e) => setForm({ ...form, price: Number(e.target.value) })} /></label>
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
