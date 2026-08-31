"""修启动脚本：把 PYTHONPATH 显式加进去，避免 subprocess 找不到 backend。"""
from pathlib import Path

p = Path("scripts/_start_uvicorn.py")
src = p.read_text(encoding="utf-8")
src = src.replace(
    'env["PYTHONDONTWRITEBYTECODE"] = "1"',
    'env["PYTHONDONTWRITEBYTECODE"] = "1"\nenv["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")',
)
p.write_text(src, encoding="utf-8")
print("OK")
