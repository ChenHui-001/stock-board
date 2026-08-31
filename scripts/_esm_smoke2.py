"""更省事的 ESM smoke：通过 curl 检查 app.js 的所有 import URL 是否 200（不强求 Node 跑模块）。"""
import re
import subprocess
import sys
from pathlib import Path

JS_DIR = Path("frontend/static/js")
BASE = "http://127.0.0.1:18765"

files = sorted(JS_DIR.glob("*.js"))
files = [f for f in files if not f.name.startswith("_")]  # 排除备份目录

print(f"=== 检查 {len(files)} 个 JS 文件的 import URL 是否 200 ===\n")

all_ok = True
for f in files:
    text = f.read_text(encoding="utf-8")
    imports = re.findall(r"import\s+(?:\{[^}]+\}|\*\s+as\s+\w+|\w+)\s+from\s+['\"]([^'\"]+)['\"]", text)
    if not imports:
        print(f"[OK]  {f.name}: 无 import")
        continue
    print(f"\n{f.name} ({len(imports)} imports):")
    for imp in imports:
        if imp.startswith("./") or imp.startswith("../"):
            url = f"{BASE}/static/js/{imp.lstrip('./')}"
        else:
            url = f"{BASE}{imp}"
        r = subprocess.run(["curl", "-sI", "-o", "NUL", "-w", "%{http_code}", url],
                          capture_output=True, text=True, timeout=5)
        code = r.stdout.strip()
        flag = "OK" if code == "200" else "BAD"
        if code != "200":
            all_ok = False
        print(f"  [{flag}] {code} {imp} → {url}")

print(f"\n{'全部 OK' if all_ok else '存在 BAD'}")
sys.exit(0 if all_ok else 1)
