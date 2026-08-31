"""给 backend/main.py 加一个 ASGI 中间件，对 /static/js/* 的响应覆盖 Cache-Control。

原因：StaticFiles 默认给所有静态资源打 `public, max-age=31536000, immutable`，
但 ESM 模块（无 build step、原生浏览器 import）会被 import 路径锁住永久缓存，
更新看不见。/static/js/* 必须每次 revalidate 才能让 index.html 的 ?v= 真正起到
强制刷新依赖模块的作用；其他静态资源（vendor/echarts、css）维持 immutable 以获
得生产期强缓存收益。
"""
from pathlib import Path

p = Path("backend/main.py")
src = p.read_text(encoding="utf-8")

# 在 FRONTEND_DIR 常量后插入中间件定义
old_anchor = '''log = logging.getLogger("main")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"'''

new_block = '''log = logging.getLogger("main")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


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
            # 包装 send：捕获响应头，改 Cache-Control
            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers") or [])
                    # 删除原 Cache-Control，再追加 no-cache
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
    raise SystemExit("未找到锚点 log / FRONTEND_DIR")
src = src.replace(old_anchor, new_block, 1)

# 在 app = FastAPI(...) 后插入 add_middleware
old_app_init = 'app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)'
new_app_init = old_app_init + '\napp.add_middleware(_JSCacheControlMiddleware)'
if old_app_init not in src:
    raise SystemExit("未找到 app = FastAPI(...) 行")
src = src.replace(old_app_init, new_app_init, 1)

p.write_text(src, encoding="utf-8")
print("OK 已注入 _JSCacheControlMiddleware")
