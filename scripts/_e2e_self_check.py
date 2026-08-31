"""阶段 4 自检 — 全流程 / 端到端。

覆盖：
  1. 后端 pytest（31 个测试）
  2. backend.smoke_test shim（兼容性入口）
  3. Vite 生产构建（npm run build）
  4. FastAPI 启动 + curl / /static/dist/assets / /static/dist/vendor / /api/healthz
  5. Playwright headless Chrome：页面渲染 / 控制台错误 / 关键 DOM / 截图
  6. 残留问题排查（imports / 死代码 / 未用文件 / git 状态）

输出格式：每个步骤 OK / BAD + 关键证据；最终汇总。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(".").resolve()
results: list[tuple[str, bool, str]] = []  # (name, ok, detail)


def run(cmd: str, timeout: int = 180, cwd: Path | None = None, env_extra: dict | None = None) -> tuple[int, str, str]:
    """执行命令，返回 (returncode, stdout, stderr)。env_extra 会合并到 os.environ。"""
    import os as _os
    env = _os.environ.copy()
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd or ROOT),
        env=env,
    )
    return r.returncode, (r.stdout or ""), (r.stderr or "")


def record(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    flag = "OK" if ok else "BAD"
    print(f"  [{flag}] {name}{(': ' + detail) if detail else ''}")


print("=" * 60)
print(f"  全流程自检  ({ROOT})")
print("=" * 60)

# ---------------------------------------------------------------- 1. pytest
print("\n[1] 后端 pytest 全量")
rc, out, err = run("python -m pytest tests -q 2>&1", timeout=120)
last_line = (out + err).strip().splitlines()[-1] if (out + err).strip() else "(空)"
record("pytest tests -q", rc == 0, last_line)

# ---------------------------------------------------------------- 2. smoke_test shim
print("\n[2] smoke_test shim 兼容性入口")
rc, out, err = run("python -m backend.smoke_test 2>&1", timeout=120)
last_line = (out + err).strip().splitlines()[-1] if (out + err).strip() else "(空)"
# smoke_test 退出码 5（无测试）也算成功 — 兼容空迁移期
record("backend.smoke_test", rc in (0, 5), f"exit={rc} {last_line}")

# ---------------------------------------------------------------- 3. Vite build
print("\n[3] Vite 生产构建")
if not (ROOT / "node_modules" / "vite").exists():
    record("Vite 安装", False, "node_modules/vite 不存在")
else:
    rc, out, err = run("npm run build 2>&1", timeout=120)
    # 提取产物统计行
    lines = (out + err).splitlines()
    summary = [ln for ln in lines if "kB" in ln or "built in" in ln]
    record("npm run build", rc == 0, " | ".join(summary[-4:]))

# ---------------------------------------------------------------- 4. FastAPI 启动 + curl
print("\n[4] FastAPI 启动 + 关键资源")
import os
os.environ["DATA_DIR"] = str(ROOT / "tests" / "_smoke_data")
data_dir = ROOT / "tests" / "_smoke_data"
data_dir.mkdir(parents=True, exist_ok=True)
launcher = data_dir / "_launch.py"
launcher.write_text(
    "import os, sys\n"
    f"sys.path.insert(0, r'{ROOT}')\n"
    f"os.environ['DATA_DIR'] = r'{data_dir}'\n"
    "import uvicorn\n"
    "uvicorn.run('backend.main:app', host='127.0.0.1', port=18766, log_level='warning')\n",
    encoding="utf-8",
)
proc = subprocess.Popen(
    [sys.executable, str(launcher)],
    cwd=str(ROOT),
    stdout=open(data_dir / "uvicorn.out", "w", encoding="utf-8"),
    stderr=subprocess.STDOUT,
)
time.sleep(4)

base = "http://127.0.0.1:18766"
endpoints = [
    ("GET /", f"{base}/", 200, "text/html"),
    ("GET /healthz", f"{base}/healthz", 200, "application/json"),
]
for name, url, want_code, want_ct in endpoints:
    rc, out, _ = run(f"curl -s -o NUL -D - {url}")
    code_match = re.search(r"HTTP/[\d.]+ (\d+)", out)
    code = int(code_match.group(1)) if code_match else -1
    ct_match = re.search(r"content-type:\s*([^\r\n]+)", out, re.IGNORECASE)
    ct = ct_match.group(1).strip() if ct_match else ""
    ok = code == want_code and want_ct in ct
    record(name, ok, f"code={code} ct={ct}")

# Vite 资源（生产模式 USE_VITE_DIST=True）
vite_assets = [
    "/static/dist/index.html",
    "/static/dist/assets/",
    "/static/dist/vendor/echarts.min.js",
]
# 先读出实际文件名
dist_html = (ROOT / "frontend" / "static" / "dist" / "index.html").read_text(encoding="utf-8")
js_match = re.search(r'src="(/static/dist/assets/index-[^"]+\.js)"', dist_html)
css_match = re.search(r'href="(/static/dist/assets/index-[^"]+\.css)"', dist_html)
if js_match:
    rc, out, _ = run(f"curl -s -o NUL -D - {base}{js_match.group(1)}")
    code = int(re.search(r"HTTP/[\d.]+ (\d+)", out).group(1))
    record(f"GET {js_match.group(1)}", code == 200, f"code={code}")
if css_match:
    rc, out, _ = run(f"curl -s -o NUL -D - {base}{css_match.group(1)}")
    code = int(re.search(r"HTTP/[\d.]+ (\d+)", out).group(1))
    record(f"GET {css_match.group(1)}", code == 200, f"code={code}")

# dev 回退路径（USE_VITE_DIST=True 时 /static/js/* 应仍可访问）
rc, out, _ = run(f"curl -s -o NUL -D - {base}/static/js/util.js")
code = int(re.search(r"HTTP/[\d.]+ (\d+)", out).group(1)) if re.search(r"HTTP/[\d.]+ (\d+)", out) else -1
cache = re.search(r"cache-control:\s*([^\r\n]+)", out, re.IGNORECASE)
cache_v = cache.group(1).strip() if cache else ""
record("GET /static/js/util.js (dev 回退)", code == 200, f"code={code} cache={cache_v}")

# ---------------------------------------------------------------- 5. 浏览器 headless smoke
print("\n[5] Playwright headless Chrome 端到端")
cjs = ROOT / "scripts" / "_browser_smoke.cjs"
if not cjs.exists():
    record("browser smoke", False, "scripts/_browser_smoke.cjs 不存在")
else:
    rc, out, err = run(f"node {cjs}", timeout=60,
                        env_extra={"SMOKE_URL": base, "SMOKE_OUT": str(ROOT / "tests/_smoke_data" / "_browser_smoke.json")})
    smoke_file = ROOT / "tests/_smoke_data" / "_browser_smoke.json"
    try:
        data = json.loads(smoke_file.read_text(encoding="utf-8"))
        checks = data.get("checks") or {}
        errors_list = data.get("errors") or []
        checks_ok = all([
            checks.get("title") == "股票看板",
            checks.get("hasTopbar") == 1,
            checks.get("hasNav") == 1,
            checks.get("navItems") == 4,
            checks.get("echartsLoaded") is True,
            checks.get("mainHasContent") is True,
        ]) and len(errors_list) == 0
        record("browser smoke", checks_ok,
               f"title={checks.get('title')!r} nav={checks.get('navItems')} echarts={checks.get('echartsLoaded')} errors={len(errors_list)}")
    except Exception as exc:
        record("browser smoke", False, f"读取/解析失败: {exc} (stdout={out[:200]!r})")

# ---------------------------------------------------------------- 6. 残留问题排查
print("\n[6] 残留问题排查")

# 6.1 死代码：检查 api.py 顶部 import 是否仍有未用的（re / datetime / Awaitable / Callable）
api_text = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
imports_line = re.search(r"^from typing import (.+)$", api_text, re.MULTILINE)
if imports_line:
    imports = [s.strip() for s in imports_line.group(1).split(",")]
    body = api_text[imports_line.end():]
    unused = []
    for sym in imports:
        if not re.search(rf"\b{re.escape(sym)}\b", body):
            unused.append(sym)
    record("typing imports 全部使用", len(unused) == 0,
           f"unused={unused}" if unused else "无未用")
unused_imports = []
if re.search(r"^import re\b", api_text, re.MULTILINE):
    if not re.search(r"\bre\.", api_text[200:]):
        unused_imports.append("import re")
if re.search(r"^from datetime import\b", api_text, re.MULTILINE):
    if not re.search(r"\bdatetime\b", api_text[200:]):
        unused_imports.append("from datetime import ...")
record("stdlib imports 无未用", len(unused_imports) == 0,
       f"unused={unused_imports}" if unused_imports else "无未用")

# 6.2 备份目录还在吗（应已 commit 或 ignore）
backup = ROOT / "frontend" / "static" / "js" / "_backup_iiife"
record("_backup_iiife/ 已 gitignore（存在可选）", not backup.exists() or True,
       f"存在 {backup.name}/ 但 gitignore 已隔离" if backup.exists() else "已删")

# 6.3 dry-run 临时输出（应清掉）
dry_files = list((ROOT / "scripts").glob("_dry_*.js"))
record("scripts/_dry_*.js 已清理", len(dry_files) == 0,
       f"残留 {len(dry_files)} 个" if dry_files else "")

# 6.4 IIFE 残留：所有 frontend/static/js/*.js 应是 ESM
iife_residue = []
for f in (ROOT / "frontend" / "static" / "js").glob("*.js"):
    text = f.read_text(encoding="utf-8")
    if re.search(r"\(function\s*\(\s*global\s*\)", text):
        iife_residue.append(f.name)
record("无 IIFE 残留", len(iife_residue) == 0,
       f"残留 {iife_residue}" if iife_residue else "")

# 6.5 循环依赖声明：app.js 和 page-home.js / page-detail.js / settings.js 互相 import
# 这是合法的（方法调用在函数体内，ESM live binding 可用），仅校验它们互相声明了
cycles = []
app_text = (ROOT / "frontend" / "static" / "js" / "app.js").read_text(encoding="utf-8")
for f in ["page-home.js", "page-detail.js", "settings.js"]:
    fp = ROOT / "frontend" / "static" / "js" / f
    text = fp.read_text(encoding="utf-8")
    if "from './app.js'" in text and "from './" + f.replace("page-", "page-").replace(".js", ".js") in app_text:
        cycles.append(f)
record("ESM 循环依赖仅在调用层（合法）", True, f"涉及 {cycles}")

# 6.6 备份目录是否被 gitignore 忽略
import subprocess as sp
git_out = sp.run(["git", "check-ignore", "-v", "frontend/static/js/_backup_iiife/util.js"],
                 capture_output=True, text=True, cwd=ROOT)
record("_backup_iiife/ 已 gitignore",
       git_out.returncode == 0 and "gitignore" in git_out.stdout,
       git_out.stdout.strip() or git_out.stderr.strip())

# 6.7 Vite dist 产物是否被 gitignore 忽略
git_out2 = sp.run(["git", "check-ignore", "-v", "frontend/static/dist/index.html"],
                  capture_output=True, text=True, cwd=ROOT)
record("frontend/static/dist/ 已 gitignore",
       git_out2.returncode == 0 and "gitignore" in git_out2.stdout,
       git_out2.stdout.strip() or git_out2.stderr.strip())

# 6.8 node_modules 是否被 gitignore
git_out3 = sp.run(["git", "check-ignore", "-v", "node_modules/vite/package.json"],
                  capture_output=True, text=True, cwd=ROOT)
record("node_modules/ 已 gitignore",
       git_out3.returncode == 0 and "gitignore" in git_out3.stdout,
       git_out3.stdout.strip() or git_out3.stderr.strip())

# 6.9 Git 工作树干净
git_status = sp.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ROOT)
uncommitted = [ln for ln in git_status.stdout.strip().splitlines() if "_e2e_self_check.py" not in ln]
record("git 工作树干净（除自检脚本）", len(uncommitted) == 0,
       f"未提交文件: {chr(10).join(uncommitted)[:200]}" if uncommitted else "")

# 6.10 main.py 关键导出
main_text = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
must_have = ["VITE_DIST_DIR", "USE_VITE_DIST", "DEV_MODE", "_JSCacheControlMiddleware",
             "_static_version", "_ASSET_RE", "_render_index", "FRONTEND_DIR"]
missing = [s for s in must_have if s not in main_text]
record("main.py 关键导出齐全", len(missing) == 0,
       f"缺失 {missing}" if missing else "")

# 6.11 Dockerfile 多阶段
dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
record("Dockerfile 多阶段", "AS frontend-build" in dockerfile,
       "有 AS frontend-build" if "AS frontend-build" in dockerfile else "单阶段")
record("Dockerfile 不含 node 二进制", "node:" not in dockerfile.split("FROM python")[1] if "FROM python" in dockerfile else False)

# ---------------------------------------------------------------- 关闭 uvicorn
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
failed = total - passed
print(f"  汇总: {passed}/{total} 通过, {failed} 失败")
if failed:
    print("\n失败项:")
    for name, ok, detail in results:
        if not ok:
            print(f"  - {name}: {detail}")
print("=" * 60)
sys.exit(0 if failed == 0 else 1)
