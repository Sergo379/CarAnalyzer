# CarAnalyzer

Локальное веб-приложение для поиска и анализа автомобильных конкурентов. Реализован
Foundation: FastAPI, типизированные модели, конфигурация ценовых диапазонов,
SQLite-схема, нормализация, `CompetitorEngine` и интерфейсы будущих интеграций.

## Запуск

Требуются Python 3.13 и `uv`.

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv sync
uv run python -m backend.database.init_db
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Проверка: `GET http://127.0.0.1:8000/health`.

```powershell
uv run pytest
uv run ruff check .
```

`POST /api/search` пока не обращается к площадкам и намеренно не возвращает фиктивные
объявления. Первый подключаемый источник — Auto.ru.
