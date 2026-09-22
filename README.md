# CarAnalyzer

Локальное веб-приложение для поиска и анализа автомобильных конкурентов. Backend
получает свежую выдачу Auto.ru/Drom, переживает частичный отказ источников, нормализует
и классифицирует объявления. React-интерфейс показывает все результаты и статистику.

Форма использует searchable-поля марки, зависимой модели и региона, а также фильтры
кузова и коробки. Справочник марок/моделей хранится в расширяемом structured JSON;
неизвестные значения по-прежнему можно ввести вручную для generic fallback.

Кандидаты берутся из широкого поиска площадок по диапазону 80–120% исходной цены,
совместимым кузовам, выбранной коробке и региону — марка и модель исходного автомобиля
не ограничивают конкурентную выдачу. Москва и Московская область представлены отдельным
region scope; Auto.ru фильтруется региональным URL, Drom — региональным URL и локальным
guard. Объявления с неизвестной коробкой исключаются при выборе конкретной коробки.
Каталог SQLite используется только для расширяемой классификации и knowledge-профилей;
неизвестная модель получает provisional-профиль и не требует AI для поиска.

Ответ `/api/search` отдельно возвращает `source_listings` и `source_model_group` — live
объявления введённой модели, разницу с введённой ценой и статистику min/avg/max. Эти
объявления не смешиваются с `direct`, `expensive` и `cheaper`.

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

## macOS Apple Silicon

Если файлы были переданы не через Git, один раз выполните
`chmod +x install.command start.command update.command`. Затем запустите
`install.command` и используйте `start.command`. Для безопасного
обновления без удаления `.env` и SQLite knowledge DB предусмотрен `update.command`.

`source_status` различает `captcha_required`, `http_429`, `http_403`, `parse_error` и
другие состояния. Avito на текущей сети отвечает HTTP 429 с причиной «проблема с IP»
даже в isolated Playwright context. Drom после серии запросов также может включить
HTTP 429 rate limit. Отказ одного источника не мешает показать данные остальных.
AI/RAG synthesis не вызывается без отдельно настроенных credentials.
