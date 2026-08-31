"""提取 backend/main.py 中与前端资源相关的代码。"""
from pathlib import Path
import re

text = Path("backend/main.py").read_text(encoding="utf-8")
lines = text.splitlines()

print(f"=== main.py 共 {len(lines)} 行 ===\n")
for i, ln in enumerate(lines, 1):
    if re.search(r"(_ASSET|_render|frontend|index\.html|static|version|StaticFiles|mount|html\.|app\.get)", ln, re.IGNORECASE):
        print(f"L{i}: {ln[:140]}")
