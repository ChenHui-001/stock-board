"""完整重写：每步先在内存中替换，最后统一 write_text。"""
from pathlib import Path
import re

main_path = Path("backend/main.py")
main_src = main_path.read_text(encoding="utf-8")

changes = []  # (label, success) 收集，最后统一报告

# === 1. 加 VITE_DIST_DIR / USE_VITE_DIST / DEV_MODE 常量 ===
old1 = 'FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"'
new1 = '''FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
# Vite 构建产物（frontend/static/dist/），存在则用此服务前端；否则回退到源 ESM
VITE_DIST_DIR = FRONTEND_DIR / "static" / "dist"
USE_VITE_DIST = VITE_DIST_DIR.is_dir() and (VITE_DIST_DIR / "index.html").is_file()
# 开发模式（无 dist）才需要 _JSCacheControlMiddleware（生产 Vite hash 已足够破缓存）
DEV_MODE = not USE_VITE_DIST'''
if old1 in main_src:
    main_src = main_src.replace(old1, new1, 1)
    changes.append(("[1] VITE_DIST_DIR/USE_VITE_DIST/DEV_MODE", True))
else:
    changes.append(("[1] FRONTEND_DIR 锚点未找到", False))

# === 2. 改 _static_version 优先读 dist ===
old2 = '''def _static_version() -> str:
    latest = 0.0
    root = FRONTEND_DIR / "static"
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                latest = max(latest, p.stat().st_mtime)
    return str(int(latest)) if latest else "1"'''
new2 = '''def _static_version() -> str:
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
    return str(int(latest)) if latest else "1"'''
if old2 in main_src:
    main_src = main_src.replace(old2, new2, 1)
    changes.append(("[2] _static_version 优先 dist", True))
else:
    changes.append(("[2] _static_version 未找到", False))

# === 3. 扩展 _ASSET_RE 匹配 /assets/ 和 /static/dist/ ===
m = re.search(r'_ASSET_RE\s*=\s*re\.compile\(r"(.+)"\)', main_src)
if m:
    old_re = m.group(0)
    old_pattern = m.group(1)
    new_pattern = old_pattern.replace("/static/(?:css|js|vendor)/", "(?:/static/(?:css|js|vendor|dist)/|/assets/)")
    new_re = old_re.replace(old_pattern, new_pattern)
    main_src = main_src.replace(old_re, new_re, 1)
    changes.append((f"[3] _ASSET_RE: {old_pattern[:50]} -> {new_pattern[:50]}", True))
else:
    changes.append(("[3] _ASSET_RE 未找到", False))

# === 4. _render_index() 读 dist 或 frontend/index.html ===
old4 = '''    global _INDEX_HTML
    if _INDEX_HTML is None:
        _INDEX_HTML = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    return _ASSET_RE.sub(lambda m: f"{m.group(1)}?v={_ASSET_VERSION}", _INDEX_HTML)'''
new4 = '''    global _INDEX_HTML
    if _INDEX_HTML is None:
        index_path = VITE_DIST_DIR / "index.html" if USE_VITE_DIST else FRONTEND_DIR / "index.html"
        _INDEX_HTML = index_path.read_text(encoding="utf-8")
    return _ASSET_RE.sub(lambda m: f"{m.group(1)}?v={_ASSET_VERSION}", _INDEX_HTML)'''
if old4 in main_src:
    main_src = main_src.replace(old4, new4, 1)
    changes.append(("[4] _render_index 优先 dist", True))
else:
    changes.append(("[4] _render_index 未找到", False))

# === 5. _JSCacheControlMiddleware 类（如果还没有）===
middleware_class = '''

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
        await self.app(scope, receive, send)
'''
if "_JSCacheControlMiddleware" not in main_src:
    # 在 DEV_MODE 常量后插入
    if "DEV_MODE = not USE_VITE_DIST" in main_src:
        main_src = main_src.replace("DEV_MODE = not USE_VITE_DIST", "DEV_MODE = not USE_VITE_DIST" + middleware_class, 1)
        changes.append(("[5] _JSCacheControlMiddleware 类已注入", True))
    else:
        changes.append(("[5] DEV_MODE 锚点未找到", False))
else:
    changes.append(("[5] _JSCacheControlMiddleware 已存在", True))

# === 6. cache_headers 跳过 /static/js/* ===
old6 = '''    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp'''
new6 = '''    resp = await call_next(request)
    path = request.url.path
    # /static/js/* 由 _JSCacheControlMiddleware 单独设 no-cache（ESM import 锁版本）
    if path.startswith("/static/") and not path.startswith("/static/js/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp'''
if old6 in main_src:
    main_src = main_src.replace(old6, new6, 1)
    changes.append(("[6] cache_headers 跳过 JS", True))
else:
    changes.append(("[6] cache_headers 未找到", False))

# === 7. add_middleware(_JSCacheControl) 用 DEV_MODE 守护 ===
old7 = "app.add_middleware(\n    CORSMiddleware,"
new7 = '''if DEV_MODE:
    # 仅在开发模式（无 Vite dist）启用：把 /static/js/* 强制 no-cache 以破 ESM import 缓存
    app.add_middleware(_JSCacheControlMiddleware)
app.add_middleware(
    CORSMiddleware,'''
if old7 in main_src:
    main_src = main_src.replace(old7, new7, 1)
    changes.append(("[7] add_middleware DEV_MODE 守护", True))
else:
    changes.append(("[7] CORS add_middleware 锚点未找到", False))

# 统一写入
main_path.write_text(main_src, encoding="utf-8")

print("=== main.py Vite 集成结果 ===")
for label, ok in changes:
    flag = "OK" if ok else "FAIL"
    print(f"  [{flag}] {label}")
print(f"\n应用 {sum(1 for _, ok in changes if ok)}/{len(changes)} 项修改")
