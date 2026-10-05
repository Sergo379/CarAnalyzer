# CarAnalyzer

Локальное веб-приложение для поиска и анализа автомобильных конкурентов. Backend
получает свежую выдачу Auto.ru/Drom, переживает частичный отказ источников, нормализует
и классифицирует объявления. React-интерфейс показывает все результаты и статистику.

Форма использует searchable-поля марки, зависимой модели и региона, а также фильтры
кузова и коробки. Основной справочник марок/моделей синхронизируется с публичным CarsBase API;
Drom дополняет выбранные модели поколениями и двигателями по запросу. Каталог хранится
в локальном structured JSON; неизвестные значения по-прежнему
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
uv sync --extra knowledge
uv run python -m backend.database.init_db
uv run python -m backend.services.marketplace_catalog --source cars-base.ru
cd frontend
npm ci
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

## Технические знания (Task 2 / PAD Lab 1)

Отдельный слой технических знаний использует canonical brand/model/generation/engine
ID, SQLite, LlamaIndex, FTS5 + sqlite-vec и локальные multilingual embeddings.
Технический профиль кешируется постоянно, без 90-дневного TTL; это не меняет TTL
старого совместимого market-knowledge поля в `/api/search`. Рыночный запрос не
запускает RAG и не ждёт Gemini: после отображения объявлений frontend читает
только локальный кешированный профиль. Если его нет, кнопка «Запустить технический
анализ» отдельно запускает discovery, embeddings и Gemini. Даже если Gemini позже
станет доступен, автоматический запуск не включается. Ошибка провайдера/сети не
затрагивает рыночные результаты и не стирает локальные технические данные.
AI-fallback классификатора сегмента также не вызывается из обычного рыночного поиска.
Без ключа Gemini показывается статус `provider_not_configured`, а неподтверждённые
проблемы не выводятся: **нет подтверждений — нет утверждений о модели**.

Установка опциональных зависимостей, схема, источники, результаты 40-вопросной
оценки, ограничения и команды для ручной сборки описаны в
[документации PAD Lab 1](lab1/README.md). Важное ограничение: реальные
генерации Gemini и полнота технических источников ещё не прошли приёмку;
прохождение unit-тестов само по себе не означает готовность Task 2.

## Каталог марок и моделей

Frontend загружает марки через `GET /api/catalog/brands`, а модели выбранной марки —
через `GET /api/catalog/models?brand=Example%20Brand`. Метаданные снимка доступны в
`GET /api/catalog/status`. Старый `GET /api/catalog/vehicles` сохранён для совместимости.

Catalog schema v3 разделяет brand, model, generation, modification и engine. Внутренние
canonical ID не заменяются ID CarsBase: внешние ID хранятся в `source_refs.external_id`,
а доступные базовые поля — в `carsbase_metadata`. Старые Auto.ru/Drom refs остаются.
CarsBase `/status` проверяется при клиентском запуске или ручной синхронизации;
`/full` загружается только при изменении версии или явном `--force`.
Ошибочный ответ не заменяет последний рабочий локальный снимок. Все
`/api/catalog/brands` и `/api/catalog/models` читают только локальный каталог без HTTP.
Поколения и двигатели загружаются
лениво через `/api/catalog/generation-state` и `/api/catalog/engines`, имеют TTL 90 дней и
кешируются в `data/vehicle_catalog_runtime.json` отдельно от неизменяемого release seed.
Статус загрузки различает `ready`, отсутствие поколений и ошибки источника. Каталог остаётся autocomplete-помощником:
неизвестные марку и модель можно вводить вручную.

Полная проверка и пополнение каталога запускаются отдельно от пользовательского поиска:

```powershell
uv run python -m backend.tools.catalog_audit --output data/catalog_audit_report.json
uv run python -m backend.tools.catalog_enrich --resume --pace-seconds 1.5
uv run python -m backend.tools.catalog_enrich --resume --retry-failed
uv run python -m backend.tools.drom_smoke --select-only
```

Обычный catalog audit проходит все локальные модели без сетевых запросов и отдельно
показывает inventory completeness и generation enrichment. CarsBase `--audit` ниже
делает только два ограниченных API-запроса и не меняет активный каталог.
Enrichment ставит контрольную точку после каждой модели и останавливается при 429/403.
Только после полного успешного аудита runtime можно вручную перенести в release seed
командой `uv run python -m backend.tools.catalog_enrich --promote`.

Поисковая identity содержит стабильные `canonical_brand_id`, `canonical_model_id`,
`canonical_generation_id` и `canonical_modification_id`. Display name не используется
как основной ключ группировки, исключения исходной модели из конкурентов или knowledge
deduplication. Если записи нет в каталоге, создаётся provisional canonical identity.

Проверить/обновить базовый инвентарь CarsBase вручную:

```powershell
uv run python -m backend.services.marketplace_catalog --source cars-base.ru --audit
uv run python -m backend.services.marketplace_catalog --source cars-base.ru
uv run python -m backend.services.marketplace_catalog --source cars-base.ru --force
```

Команда по умолчанию теперь выбирает CarsBase. Исторические диагностики площадок доступны
явно, но не запускаются при обычной синхронизации:

```powershell
uv run python -m backend.services.marketplace_catalog --source auto.ru
uv run python -m backend.services.marketplace_catalog --source drom.ru
```

Сопоставление CarsBase аддитивно: ID, alias, source refs, поколения и статус обогащения
сохраняются; неоднозначности выносятся в dry-run отчёт. Модели, отсутствующие в CarsBase,
не удаляются. API и frontend пользуются локальным кешем даже без связи с CarsBase.
CarsBase-класс `J` даёт только SUV-family; размер подкласса требует размерных данных.

Drom для CarsBase-модели без `drom.ru` ref разрешает только выбранную модель через индекс
марки и одну страницу моделей, сохраняет точное совпадение и затем запускает прежний
generation parser. Неоднозначный/закрытый источник не блокирует Auto.ru или Avito.
При
ограничении direct HTTP scraper пробует изолированный Chromium context без личного
профиля; CAPTCHA не обходится. HTTP 429 описывается как ограничение автоматического
доступа, а не как доказательство блокировки IP.

Происхождение базовых данных: [CarsBase API](https://api.cars-base.ru) и
[проект carsBase](https://github.com/blanzh/carsBase). В upstream репозитории не обнаружена
явная лицензия на коммерческое распространение. Право на коммерческое использование и
перераспространение нужно подтвердить отдельно до поставки клиенту; CarsBase dataset в
release seed этой задачей не включается.

Auto.ru также использует catalog source mapping; если конкретного source ref пока нет,
адаптер безопасно переходит к выдаче марки и применяет canonical/local guard. Auto.ru и
Drom следуют явной ссылке следующей страницы до её исчезновения, отсутствия новых ID или
настраиваемых safety caps `scraper_max_pages`/`scraper_max_listings`.

Безопасный debug mode включается полем `debug: true` в `/api/search`. В ответе появляются
только URL запроса и счётчики `pages_scanned`, `raw_count`, `parsed_count`,
`accepted_count`, `rejected`; cookies, заголовки авторизации и credentials не собираются.

## macOS Apple Silicon: клиентский запуск

Клиентский пакет — ZIP из чистого Git-клона стабильной ветки с сохранённым каталогом
`.git`. В пакет не включают `.env`/ключи, виртуальное окружение, `node_modules`,
`frontend/dist`, логи, pytest scratch, временные базы и developer cache. Скрипты
`.command` должны сохранять Unix LF и исполняемый бит; если ZIP потерял разрешения,
один раз выполните `chmod +x install.command start.command update.command`.

1. Распакуйте пакет и один раз запустите `install.command`. Он проверит macOS arm64,
   при наличии Homebrew установит недостающие uv/Node, установит Python 3.13 и
   locked-зависимости (включая технический pipeline), Chromium для Playwright,
   создаст `.env` только если его нет, безопасно инициализирует SQLite, соберёт frontend,
   проверит CarsBase и `/health`. API-ключ Gemini для установки не нужен.
2. Для обычной работы запустите `start.command`. Он проверит CarsBase `/status`
   и при необходимости обновит локальный инвентарь; недоступность CarsBase оставляет
   последний локальный каталог. Скрипт запускает один backend на `127.0.0.1:8000`,
   открывает браузер после `/health`, остаётся в Terminal и пишет `logs/backend.log`.
   Закрытие этого Terminal или Ctrl+C останавливает backend; фонового демона и
   `stop.command` нет.
3. Когда выпущена новая стабильная версия, закройте приложение и запустите
   `update.command`. Требуются `.git`, чистые локальные исходники и настроенный
   upstream текущей стабильной ветки. Обновление не переключает ветку: выполняет
   `git fetch`, `git pull --ff-only`, до миграции создаёт согласованные SQLite-бэкапы
   в `data/backups/`, затем обновляет зависимости, Chromium, схему БД и frontend.
   `.env`, SQLite, runtime-каталог и browser runtime не удаляются. Если исходники
   менялись локально, обновление останавливается без reset/merge.

Поставка клиенту требует отдельной проверки прав на перераспространение CarsBase.
Перед созданием ZIP убедитесь, что в Git не отслеживаются developer scratch-файлы:
`.gitignore` не удаляет уже отслеживаемые файлы из будущего клона.

`source_status` различает `captcha_required`, `http_429`, `http_403`,
`browser_access_limited`, `parse_error` и другие состояния. При HTTP 403/429 Avito и
Drom проверяют доступ через isolated Playwright context без личного browser profile.
На текущей сети Avito ограничивает и этот путь; это не интерпретируется как постоянная
блокировка IP. Отказ одного источника не мешает показать данные остальных.
AI/RAG synthesis не вызывается без отдельно настроенных credentials.
