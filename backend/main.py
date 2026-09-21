from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from backend.api.search import router as search_router
from backend.config import get_settings
from backend.database.init_db import initialize_database


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(search_router)


@app.get("/health", tags=["system"])
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": settings.app_name, "version": app.version}
