"""把 _JSCacheControlMiddleware 重新加进 main.py，并包成 dev-mode 守护。"""
from pathlib import Path

p = Path("backend/main.py")
src = p.read_text(encoding="utf-8")

# 1. 在 FRONTEND_DIR 后追加中间件类定义（如果还没加）
old_anchor = '''log = logging.getLogger("main")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
# Vite 构建产物（frontend/static/dist/），存在则用此服务前端；否则回退到源 ESM
VITE_DIST_DIR = FRONTEND_DIR / "static" / "dist"
USE_VITE_DIST = VITE_DIST_DIR.is_dir() and (VITE_DIST_DIR / "index.html").is_file()
# 开发模式（无 dist）才需要 _JSCacheControlMiddleware（生产 Vite hash 已足够破缓存）
DEV_MODE = not USE_VITE_DIST'''

new_block = '''log = logging.getLogger("main")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
# Vite 构建产物（frontend/static/dist/），存在则用此服务前端；否则回退到源 ESM
VITE_DIST_DIR = FRONTEND_DIR / "static" / "dist"
USE_VITE_DIST = VITE_DIST_DIR.is_dir() and (VITE_DIST_DIR / "index.html").is_file()
# 开发模式（无 dist）才需要 _JSCacheControlMiddleware（生产 Vite hash 已足够破缓存）
DEV_MODE = not USE_VITE_DIST


class _JSCacheControlMiddleware:
    """覆写 /static/js/* 的 Cache-Control：no-cache（强制 revalidate）。

    ESM 模式下 app.js 内部 `import './util.js'` 走无版本号 URL，
    原生浏览器拿到 immutable 缓存后即使文件改了也看不见。把 JS 路径降级为
    no-cache（仍可走强缓存，仅多一次 304 revalidate），其他静态资源保持
    默认 immutable 以保留强缓存收益。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/static/js/"):
            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers") or [])
                    new_headers = [
                        (k, v) for (k, v) in headers
                        if k.lower() != b"cache-control"
                    ]
                    new_headers.append((b"cache-control", b"no-cache"))
                    message["headers"] = new_headers
                await send(message)
            await self.app(scope, receive, send_wrapper)
            return
        await self.app(scope, receive, send)'''

if old_anchor not in src:
    raise SystemExit("未找到 FRONTEND_DIR 锚点")
src = src.replace(old_anchor, new_block, 1)

# 2. 修改 cache_headers：跳过 /static/js/*
old_cache = '''    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp'''

new_cache = '''    resp = await call_next(request)
    path = request.url.path
    # /static/js/* 由 _JSCacheControlMiddleware 单独设 no-cache（ESM import 锁版本）
    if path.startswith("/static/") and not path.startswith("/static/js/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp'''

if old_cache not in src:
    raise SystemExit("未找到 cache_headers 函数体")
src = src.replace(old_cache, new_cache, 1)

# 3. 在 app.add_middleware(CORSMiddleware,...) 之前插入 dev-mode 中间件注册
old_cors = '''app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,'''

new_cors = '''app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)
if DEV_MODE:
    # 仅在开发模式（无 Vite dist）启用：把 /static/js/* 强制 no-cache 以破 ESM import 缓存
    app.add_middleware(_JSCacheControlMiddleware)
app.add_middleware(
    CORSMiddleware,'''

if old_cors not in src:
    raise SystemExit("未找到 CORS add_middleware 锚点")
src = src.replace(old_cors, new_cors, 1)

p.write_text(src, encoding="utf-8")
print("OK 已注入 _JSCacheControlMiddleware + dev-mode 守护 + cache_headers 跳过 JS")
