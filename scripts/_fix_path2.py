"""修 ESM smoke：直接用绝对 URL 引用 app.js。"""
from pathlib import Path

p = Path("scripts/_esm_smoke.py")
src = p.read_text(encoding="utf-8")
old = "const appPath = new URL('../frontend/static/js/app.js', import.meta.url).href;\nconst mod = await import(appPath);"
new = "import { pathToFileURL } from 'node:url';\nconst appPath = pathToFileURL('E:/project/股票看板/frontend/static/js/app.js').href;\nconst mod = await import(appPath);"
src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("OK")
