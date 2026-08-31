"""小启动脚本：起 uvicorn 到后台端口 18765（DATA_DIR 临时化）。"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("E:/project/股票看板")
TMP_DATA = ROOT / "tests/_smoke_data"
TMP_DATA.mkdir(parents=True, exist_ok=True)

env = os.environ.copy()
env["DATA_DIR"] = str(TMP_DATA)
env["PYTHONUNBUFFERED"] = "1"
env["PYTHONDONTWRITEBYTECODE"] = "1"
env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")

# 写到文件再 exec，避免 PowerShell 转义
script = TMP_DATA / "_launch.py"
script.write_text(
    "import os, sys\n"
    f"os.environ['DATA_DIR'] = r'{TMP_DATA}'\n"
    "import uvicorn\n"
    "uvicorn.run('backend.main:app', host='127.0.0.1', port=18765, log_level='warning')\n",
    encoding="utf-8",
)

proc = subprocess.Popen(
    [sys.executable, str(script)],
    env=env,
    cwd=str(ROOT),
    stdout=open(TMP_DATA / "uvicorn.out", "w", encoding="utf-8"),
    stderr=subprocess.STDOUT,
)
print(f"uvicorn PID={proc.pid}")
import time
time.sleep(5)
print("slept 5s")
# Write PID for later kill
(TMP_DATA / "uvicorn.pid").write_text(str(proc.pid), encoding="utf-8")
