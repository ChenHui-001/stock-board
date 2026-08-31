"""验证 api.py 可被 Python 解析，并打印文件大小/行数。"""
import ast
import os
from pathlib import Path

api = Path("backend/api.py")
src = api.read_text(encoding="utf-8")
ast.parse(src)
size = api.stat().st_size
lines = src.count("\n") + 1
print(f"OK api.py 可解析: {lines} 行 / {size} 字节")
