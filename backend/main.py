"""FastAPI 入口：挂载 API 与前端静态资源，单容器同时提供两者。"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
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

# 静态资源版本号：取 static 目录下最新文件的 mtime，前端文件一变版本即变。
# index.html 给 css/js/vendor 加上 ?v= 查询参数，/static 响应头 immutable 长缓存；
# 升级后浏览器自动拉新文件，不再需要手动 Ctrl+F5 强刷。
def _static_version() -> str:
    latest = 0.0
    root = FRONTEND_DIR / "static"
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                latest = max(latest, p.stat().st_mtime)
    return str(int(latest)) if latest else "1"


_ASSET_VERSION = _static_version()
_INDEX_HTML: str | None = None
_ASSET_RE = re.compile(r"(/static/(?:css|js|vendor)/[^\"'?#\s]+)")


def _render_index() -> str:
    global _INDEX_HTML
    if _INDEX_HTML is None:
        _INDEX_HTML = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    return _ASSET_RE.sub(lambda m: f"{m.group(1)}?v={_ASSET_VERSION}", _INDEX_HTML)


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


@app.middleware("http")
async def cache_headers(request, call_next):
    """HTML 始终 no-cache（配合 ?v= 保证前端改动即时生效）；静态资源 immutable 长缓存。"""
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    """Prometheus 指标端点：单源抓取请求/熔断/耗时等可观测性数据。

    文本格式（text/plain; version=0.0.4），由 Prometheus 服务端定期抓取。
    CORS 已开 *，跨域抓取无需额外头。
    """
    from . import metrics as _metrics
    body, content_type = _metrics.export()
    return Response(content=body, media_type=content_type)


if FRONTEND_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(FRONTEND_DIR / "static")),
        name="static",
    )

    @app.get("/")
    async def index() -> Response:
        return Response(
            _render_index(),
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/favicon.svg")
    async def favicon() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "static" / "favicon.svg"))
