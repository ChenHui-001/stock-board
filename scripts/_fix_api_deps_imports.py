"""把 api_deps.py 中错误的相对 import (..) 改成单层相对 import (.)。

原因：api_deps.py 在 backend/ 包内，正确的相对 import 应是 '.'（指 backend 包本身）。
之前 .. 会导致 ImportError: attempted relative import beyond top-level package。
"""
from pathlib import Path

p = Path("backend/api_deps.py")
src = p.read_text(encoding="utf-8")

fixes = [
    ("from .. import llmcfg, scorecfg", "from . import llmcfg, scorecfg"),
    ("from ..storage import get_report, is_watched", "from .storage import get_report, is_watched"),
    ("from ..utils import describe_exc, is_trading_now", "from .utils import describe_exc, is_trading_now"),
    ("    from ..config import settings", "    from .config import settings"),
]

for old, new in fixes:
    if old not in src:
        print(f"[MISS] {old!r}")
        continue
    if src.count(old) != 1:
        print(f"[DUP] {old!r} 出现 {src.count(old)} 次")
        continue
    src = src.replace(old, new)
    print(f"[OK] {old!r} -> {new!r}")

p.write_text(src, encoding="utf-8")
print("\n完成。可用 python -c 'from backend import api' 自检。")
