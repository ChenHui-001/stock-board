"""改 vite.config.js 加 base: '/static/dist/'，让 Vite 输出的资源 URL 自带 /static/dist/ 前缀。

这样 Vite 输出的 index.html 里就是 /static/dist/assets/... 而非 /assets/...，
与 main.py 已有的 /static/ 挂载点直接兼容（frontend/static/dist/assets/... → /static/dist/assets/）。"""
from pathlib import Path

p = Path("vite.config.js")
src = p.read_text(encoding="utf-8")
src = src.replace(
    "export default defineConfig({\n  root: FRONTEND,",
    "export default defineConfig({\n  base: '/static/dist/',\n  root: FRONTEND,",
)
p.write_text(src, encoding="utf-8")
print("OK")
