"""FastAPI 入口：挂载 API 与前端静态资源，单容器同时提供两者。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import llm, storage
from .api import router
from .config import settings
from .providers import shutdown as providers_shutdown

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("main")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    storage.purge_old_reports()
    log.info(
        "启动 %s | 数据源=%s | AI=%s",
        settings.APP_NAME,
        ",".join(settings.PROVIDER_ORDER),
        settings.LLM_MODEL if llm.available() else "内置规则引擎",
    )
    yield
    await providers_shutdown()
    await llm.close()


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


if FRONTEND_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(FRONTEND_DIR / "static")),
        name="static",
    )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/favicon.svg")
    async def favicon() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "static" / "favicon.svg"))
