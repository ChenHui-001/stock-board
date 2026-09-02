"""前端 ESM 冒烟入口（转发给 scripts/frontend_smoke.mjs）。

旧实现只把 app.js 当 ESM 导入，DOM stub 太薄，实际会崩在 settings.js 的
getElementById('cfg-add') 上（返回 null 再点 .addEventListener），也就是说这个
脚本长期是坏的、没人发现。真正的冒烟已经用 Node 重写在 frontend_smoke.mjs 里：
假 DOM + 假 fetch 加载 app.js、驱动 route() 切路由、断言 destroy 卸载链路生效。

这里保留 .py 入口只是为了不破坏已有的调用习惯，逻辑全部在 .mjs 里。
"""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
smoke = root / "scripts" / "frontend_smoke.mjs"

r = subprocess.run(["node", str(smoke)], cwd=str(root), text=True)
sys.stdout.write(r.stdout or "")
sys.stdout.write(r.stderr or "")
sys.exit(r.returncode)
