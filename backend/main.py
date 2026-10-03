import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.knowledge import router as knowledge_router
from backend.api.search import router as search_router
from backend.config import PROJECT_ROOT, get_settings
from backend.database.init_db import initialize_database


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Uvicorn's --log-level applies to its own loggers, not application INFO events.
    knowledge_log = logging.getLogger("backend.knowledge")
    knowledge_log.setLevel(logging.INFO)
    if not knowledge_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        knowledge_log.addHandler(handler)
    knowledge_log.propagate = False
    initialize_database()
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.include_router(search_router)
app.include_router(knowledge_router)


@app.get("/health", tags=["system"])
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": settings.app_name, "version": app.version}


frontend_dist = PROJECT_ROOT / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
