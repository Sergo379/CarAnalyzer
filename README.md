# CarAnalyzer

Локальное веб-приложение для поиска и анализа автомобильных конкурентов. Backend
получает свежую выдачу Auto.ru/Drom, переживает частичный отказ источников, нормализует
и классифицирует объявления. React-интерфейс показывает все результаты и статистику.

Форма использует searchable-поля марки, зависимой модели и региона, а также фильтры
кузова и коробки. Справочник марок/моделей синхронизируется с публичными каталогами
Auto.ru и Drom и хранится в локальном structured JSON; неизвестные значения по-прежнему
можно ввести вручную для generic fallback.

Кандидаты берутся из широкого поиска площадок по диапазону 80–120% исходной цены,
совместимым кузовам, выбранной коробке и региону — марка и модель исходного автомобиля
не ограничивают конкурентную выдачу. Москва и Московская область представлены отдельным
region scope; Auto.ru фильтруется региональным URL, Drom — региональным URL и локальным
guard. Объявления с неизвестной коробкой исключаются при выборе конкретной коробки.
Каталог SQLite используется только для расширяемой классификации и knowledge-профилей;
неизвестная модель получает provisional-профиль и не требует AI для поиска.

Ответ `/api/search` отдельно возвращает `source_listings` и `source_model_group` — live
объявления введённой модели, разницу с введённой ценой и статистику min/avg/max. Эти
объявления не смешиваются с `direct`, `expensive` и `cheaper`. `source_distribution`
показывает число принятых объявлений по каждой площадке.

Год и цена поддерживают режимы `exact` и `range`. В диапазонном режиме исходный рынок
фильтруется по указанным границам, а reference price вычисляется как середина диапазона;
утверждённые процентные окна конкурентов при этом не меняются. Поколения выбираются по
пересечению production interval с выбранным годом или диапазоном.

## Запуск

Требуются Python 3.13 и `uv`.

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv sync
uv run python -m backend.database.init_db
cd frontend
npm install
npm run build
cd ..
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Проверка: `GET http://127.0.0.1:8000/health`.

```powershell
uv run pytest
uv run ruff check .
```

После сборки frontend приложение доступно по `http://127.0.0.1:8000`.

## Каталог марок и моделей

Frontend загружает марки через `GET /api/catalog/brands`, а модели выбранной марки —
через `GET /api/catalog/models?brand=BMW`. Метаданные снимка доступны в
`GET /api/catalog/status`. Старый `GET /api/catalog/vehicles` сохранён для совместимости.

Catalog schema v3 разделяет brand, model, generation, modification и engine. Для каждой
сущности сохраняются source-specific URL/path/slug. Поколения и двигатели загружаются
лениво через `/api/catalog/generations` и `/api/catalog/engines`, имеют TTL 90 дней и
кешируются в том же локальном снимке. Каталог остаётся autocomplete-помощником:
неизвестные марку и модель можно вводить вручную.

Поисковая identity содержит стабильные `canonical_brand_id`, `canonical_model_id`,
`canonical_generation_id` и `canonical_modification_id`. Display name не используется
как основной ключ группировки, исключения исходной модели из конкурентов или knowledge
deduplication. Если записи нет в каталоге, создаётся provisional canonical identity.

Обновить локальный кеш из обоих публичных каталогов:

```powershell
uv run python -m backend.services.marketplace_catalog
```

Можно указать один источник: `--source auto.ru` или `--source drom.ru`. Данные источников
объединяются, алиасы марок нормализуются, а при полном сетевом отказе последний рабочий
снимок остаётся неизменным.

Drom model resolver использует source refs из каталога, а не whitelist моделей. При
ограничении direct HTTP scraper пробует изолированный Chromium context без личного
профиля; CAPTCHA не обходится. HTTP 429 описывается как ограничение автоматического
доступа, а не как доказательство блокировки IP.

Auto.ru также использует catalog source mapping; если конкретного source ref пока нет,
адаптер безопасно переходит к выдаче марки и применяет canonical/local guard. Auto.ru и
Drom следуют явной ссылке следующей страницы до её исчезновения, отсутствия новых ID или
настраиваемых safety caps `scraper_max_pages`/`scraper_max_listings`.

Безопасный debug mode включается полем `debug: true` в `/api/search`. В ответе появляются
только URL запроса и счётчики `pages_scanned`, `raw_count`, `parsed_count`,
`accepted_count`, `rejected`; cookies, заголовки авторизации и credentials не собираются.

## macOS Apple Silicon

Если файлы были переданы не через Git, один раз выполните
`chmod +x install.command start.command update.command`. Затем запустите
`install.command` и используйте `start.command`. Для безопасного
обновления без удаления `.env` и SQLite knowledge DB предусмотрен `update.command`.

`source_status` различает `captcha_required`, `http_429`, `http_403`,
`browser_access_limited`, `parse_error` и другие состояния. При HTTP 403/429 Avito и
Drom проверяют доступ через isolated Playwright context без личного browser profile.
На текущей сети Avito ограничивает и этот путь; это не интерпретируется как постоянная
блокировка IP. Отказ одного источника не мешает показать данные остальных.
AI/RAG synthesis не вызывается без отдельно настроенных credentials.
