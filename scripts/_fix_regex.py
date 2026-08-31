"""修 _ASSET_RE 的正则匹配。"""
from pathlib import Path
import re

p = Path("scripts/_main_vite_integration.py")
src = p.read_text(encoding="utf-8")

old = "m = re.search(r'_ASSET_RE = re\\.compile\\(r\"([^\"]+)\"\\)', main_src)"
new = "m = re.search(r'_ASSET_RE\\s*=\\s*re\\.compile\\(r\"(.+)\"\\)', main_src)"

if old not in src:
    raise SystemExit("未找到要替换的行")
src = src.replace(old, new, 1)
p.write_text(src, encoding="utf-8")
print("OK")
