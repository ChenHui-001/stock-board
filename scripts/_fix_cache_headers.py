"""修 cache_headers：跳过 /static/js/*（由 _JSCacheControlMiddleware 单独管 no-cache）。"""
from pathlib import Path

p = Path("backend/main.py")
src = p.read_text(encoding="utf-8")

old = '''    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp'''

new = '''    resp = await call_next(request)
    path = request.url.path
    # /static/js/* 由 _JSCacheControlMiddleware 单独设 no-cache（ESM import 锁版本）
    if path.startswith("/static/") and not path.startswith("/static/js/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp'''

if old not in src:
    raise SystemExit("未找到 cache_headers 块")
src = src.replace(old, new, 1)
p.write_text(src, encoding="utf-8")
print("OK 已修正 cache_headers")
