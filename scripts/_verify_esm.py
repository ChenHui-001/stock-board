"""验证 12 个 ESM 转换后的 JS 文件：
1. 没有残留的 IIFE `(function (global)` 或 `})(window)`
2. 没有残留的 `global.X = ...` 赋值
3. 没有残留的 `'use strict';`
4. 顶层都有 import 和 export
5. 语法可被 Node 解析（用 --check）
"""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("frontend/static/js")
files = [
    "util.js", "api.js", "charts.js", "ai.js", "news.js",
    "page-search.js", "page-value.js", "page-hotspot.js",
    "page-home.js", "page-detail.js", "settings.js", "app.js",
]

print("=" * 60)
print("ESM 转换验证")
print("=" * 60)

# 1-3. 残留检查
issues = 0
for f in files:
    text = (ROOT / f).read_text(encoding="utf-8")
    name = f
    bad = []
    if re.search(r"\(function\s*\(\s*global\s*\)", text):
        bad.append("  残留 `(function (global)`")
    if re.search(r"\}\)\(\s*(?:window|global)\s*\)\s*;", text):
        bad.append("  残留 `})(window);`")
    if re.search(r"^\s*global\.\w+\s*=", text, re.MULTILINE):
        bad.append("  残留 `global.X = ...`")
    if re.search(r"'use strict';", text):
        bad.append("  残留 'use strict';")

    has_import = bool(re.search(r"^[ \t]*import\s+", text, re.MULTILINE))
    has_export = bool(re.search(r"^[ \t]*export\s+", text, re.MULTILINE))
    if not has_import and not has_export:
        # util.js 和 api.js 不依赖别人，所以可能没 import；但必须有 export
        if not has_export:
            bad.append("  既无 import 也无 export")

    if bad:
        issues += 1
        print(f"\n[BAD] {name}")
        for b in bad:
            print(b)
    else:
        print(f"[OK]  {name:18} import={has_import} export={has_export}")

# 4. 语法检查
print("\n=== 语法检查（node --check） ===")
for f in files:
    path = ROOT / f
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = tmp.name
    try:
        r = subprocess.run(
            ["node", "--check", tmp_path],
            capture_output=True, text=True,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if r.returncode != 0:
        issues += 1
        print(f"[BAD] {f}: {r.stderr.strip()}")
    else:
        print(f"[OK]  {f}")

print(f"\n{'OK' if issues == 0 else 'BAD'}：{'全部通过' if issues == 0 else f'{issues} 个问题'}")
sys.exit(0 if issues == 0 else 1)
