"""阶段 3 收尾验证：
1. api.py 不再有已迁出符号的本地定义（确认重复已彻底清干净）
2. api.py / api_deps.py 都能被导入且关键符号 re-export 正常
3. 主程序仍能 `from backend.main import app`（main.py 用 `from .api import router`）
4. api.py 顶部不再有未用 import（asyncio/re/datetime/Awaitable/Callable 已被去重）
"""
from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

print("=" * 60)
print("阶段 3 验证：api.py / api_deps.py 拆分完成度")
print("=" * 60)

# 1. 重复定义扫描
api_text = Path("backend/api.py").read_text(encoding="utf-8")
moved_symbols = [
    "_fail", "_sentiment_stats", "_AILock", "_ai_locks", "_with_ai_lock",
    "_REQUIRED_REPORT_FIELDS", "_BLANK_LLM_REASON_RE",
    "_cache_fresh", "_cached_report", "_cached_brief_report",
    "_mark_value_watched", "_ai_summary_from_report",
]
print("\n[1] 重复定义检查（应当全部 0 处）")
all_clean = True
for sym in moved_symbols:
    # 仅匹配"定义"模式：def sym(.../class sym(/sym = /sym:
    pat = re.compile(rf"^(def {sym}\b|class {sym}\b|{sym}\s*=|{sym}\s*:)", re.MULTILINE)
    matches = pat.findall(api_text)
    flag = "OK" if not matches else "BAD"
    if matches:
        all_clean = False
    print(f"    [{flag}] {sym}: {len(matches)} 处本地定义")

# 2. import 自检：api.py 不再依赖已删除的 stdlib 名
print("\n[2] import 整洁度检查")
bad_imports = []
if re.search(r"^import re\b", api_text, re.MULTILINE):
    bad_imports.append("import re")
if re.search(r"^from datetime import\b", api_text, re.MULTILINE):
    bad_imports.append("from datetime import ...")
if re.search(r"\bAwaitable\b", api_text) and "from typing import" in api_text:
    if "Awaitable" in re.search(r"from typing import ([^\n]+)", api_text).group(1):
        bad_imports.append("Awaitable 仍在 typing import")
if re.search(r"\bCallable\b", api_text) and "from typing import" in api_text:
    if "Callable" in re.search(r"from typing import ([^\n]+)", api_text).group(1):
        bad_imports.append("Callable 仍在 typing import")
if bad_imports:
    for b in bad_imports:
        print(f"    [BAD] {b}")
else:
    print("    [OK] 无残留未用 import（re/datetime/Awaitable/Callable 已清理）")

# 3. 关键符号 re-export
print("\n[3] api.* 关键符号 re-export")
sys.path.insert(0, str(Path.cwd()))
api_mod = importlib.import_module("backend.api")
must_have = [
    "REPORT_SCHEMA_VERSION",
    "_BLANK_LLM_REASON_RE",
    "_ai_locks",
    "_cache_fresh",
    "_cached_report",
    "_with_ai_lock",
    "_mark_value_watched",
    "_fail",
    "_sentiment_stats",
    "_cached_brief_report",
    "_ai_summary_from_report",
]
for sym in must_have:
    flag = "OK" if hasattr(api_mod, sym) else "BAD"
    print(f"    [{flag}] api.{sym}")

# 4. main 仍可启动（仅验证 import 路径，不跑 uvicorn）
print("\n[4] FastAPI app 仍可 import")
try:
    main_mod = importlib.import_module("backend.main")
    app = main_mod.app
    print(f"    [OK] FastAPI app 已就绪：{app.title}")
except Exception as exc:
    print(f"    [BAD] main.py 导入失败：{exc}")

# 5. 行数 / 体积对比
api_size = Path("backend/api.py").stat().st_size
deps_size = Path("backend/api_deps.py").stat().st_size
print("\n[5] 文件体积")
print(f"    backend/api.py:      {api_size:>6} 字节")
print(f"    backend/api_deps.py: {deps_size:>6} 字节")
print(f"    合计:                {api_size + deps_size:>6} 字节（原 api.py 31890 字节）")

# 6. 重复 import 警告
print("\n[6] pytest 一次冒烟")
r = subprocess.run(
    [sys.executable, "-m", "pytest", "tests", "-q"],
    capture_output=True, text=True, timeout=120,
)
last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "(空)"
print(f"    {last}")
print(f"    exit code: {r.returncode}")

print("\n" + "=" * 60)
print("验证完成。")
print("=" * 60)
