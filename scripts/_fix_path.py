"""修 ESM smoke stub：把 import 路径改成绝对路径。"""
from pathlib import Path

p = Path("scripts/_esm_smoke.py")
src = p.read_text(encoding="utf-8")
src = src.replace(
    "const mod = await import('./frontend/static/js/app.js');",
    "const appPath = new URL('../frontend/static/js/app.js', import.meta.url).href;\nconst mod = await import(appPath);",
)
p.write_text(src, encoding="utf-8")
print("OK")
