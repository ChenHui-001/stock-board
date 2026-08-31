"""在 api.py 的 api_deps import 块里加上 _BLANK_LLM_REASON_RE（无前导缩进）。"""
from pathlib import Path

p = Path("backend/api.py")
src = p.read_text(encoding="utf-8")

old = "_ai_locks,  # noqa: F401  re-exported for tests `from backend import api; api._ai_locks`"
new = "_BLANK_LLM_REASON_RE,  # noqa: F401\n_ai_locks,  # noqa: F401  re-exported for tests `from backend import api; api._ai_locks`"

if old not in src:
    raise SystemExit(f"未找到目标行: {old!r}")
src = src.replace(old, new, 1)
p.write_text(src, encoding="utf-8")
print("OK 已添加 _BLANK_LLM_REASON_RE import")
