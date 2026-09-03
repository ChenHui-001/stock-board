"""FastAPI 入口：挂载 API 与前端静态资源，单容器同时提供两者。"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

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
# Vite 构建产物（frontend/static/dist/），存在则用此服务前端；否则回退到源 ESM
VITE_DIST_DIR = FRONTEND_DIR / "static" / "dist"
USE_VITE_DIST = VITE_DIST_DIR.is_dir() and (VITE_DIST_DIR / "index.html").is_file()
# 开发模式（无 dist）才需要 _JSCacheControlMiddleware（生产 Vite hash 已足够破缓存）
DEV_MODE = not USE_VITE_DIST

# 静态资源版本号：取 static 目录下最新文件的 mtime，前端文件一变版本即变。
# index.html 给 css/js/vendor 加上 ?v= 查询参数，/static 响应头 immutable 长缓存；
# 升级后浏览器自动拉新文件，不再需要手动 Ctrl+F5 强刷。
def _static_version() -> str:
    """版本号取最近一次"前端产物改动"的 mtime。
    - 生产（有 Vite dist）：用 dist/ 下所有文件 mtime 的最大值——npm run build 后整体更新
    - 开发（无 dist）：用 static/ 源文件 mtime 的最大值
    二者统一为同一字段，注入到 index.html 的 <link>/<script> ?v= 保持现有强刷机制。
    """
    latest = 0.0
    root = VITE_DIST_DIR if USE_VITE_DIST else (FRONTEND_DIR / "static")
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                latest = max(latest, p.stat().st_mtime)
    return str(int(latest)) if latest else "1"


_ASSET_VERSION = _static_version()
_INDEX_HTML: str | None = None
_ASSET_RE = re.compile(r"((?:/static/(?:css|js|vendor|dist)/|/assets/)[^\"'?#\s]+)")


def _render_index() -> str:
    global _INDEX_HTML
    if _INDEX_HTML is None:
        index_path = VITE_DIST_DIR / "index.html" if USE_VITE_DIST else FRONTEND_DIR / "index.html"
        _INDEX_HTML = index_path.read_text(encoding="utf-8")
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
    # 最后关 DB：providers/llm 收尾期间可能还有落盘动作（如分析记录回写）
    storage.close_db()


class _JSCacheControlMiddleware:
    """开发模式专用：/static/js/* 强制 no-cache。

    历史缺陷：DEV_MODE 分支引用了本类但类定义缺失，导致「无 dist 的全新
    checkout」（CI / 干净克隆）一导入 backend.main 就 NameError；
    生产与本机（有 dist）永远走不到该分支，故长期潜伏。现补回实现。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path", "").startswith("/static/js/"):
            async def _send(message: Any) -> None:
                if message["type"] == "http.response.start":
                    MutableHeaders(scope=message)["Cache-Control"] = "no-cache"
                await send(message)

            await self.app(scope, receive, _send)
        else:
            await self.app(scope, receive, send)


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)
if DEV_MODE:
    # 仅在开发模式（无 Vite dist）启用：把 /static/js/* 强制 no-cache 以破 ESM import 缓存
    app.add_middleware(_JSCacheControlMiddleware)
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
    path = request.url.path
    # /static/js/* 由 _JSCacheControlMiddleware 单独设 no-cache（ESM import 锁版本）
    if path.startswith("/static/") and not path.startswith("/static/js/"):
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

    # /vendor：echarts 等第三方库的稳定根路径。charts.js 懒加载注入的是
    # "/vendor/echarts.min.js"（历史约定），但 /static 挂载下该文件实际位于
    # /static/public/vendor/（dev）或 /static/dist/vendor/（vite 构建），
    # 根路径 /vendor 无人服务 → 404 → 详情页"图表库未加载"。
    # 这里把 public/vendor 直接挂到 /vendor，dev 与 dist 两种模式同路径可达。
    _VENDOR_DIR = FRONTEND_DIR / "static" / "public" / "vendor"
    if _VENDOR_DIR.exists():
        app.mount(
            "/vendor",
            StaticFiles(directory=str(_VENDOR_DIR)),
            name="vendor",
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
