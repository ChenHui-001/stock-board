"""把 api_deps.py 中所有 `from ..config import settings` 改为 `from .config import settings`。"""
from pathlib import Path

p = Path("backend/api_deps.py")
src = p.read_text(encoding="utf-8")
before = src.count("from ..config import settings")
src = src.replace("from ..config import settings", "from .config import settings")
p.write_text(src, encoding="utf-8")
print(f"替换 {before} 处 `from ..config import settings` -> `from .config import settings`")
