"""把 echarts 的普通 <script> 标签加回 frontend/index.html 顶部（Vite 会从 publicDir 直接拷贝）。

Vite 规则：
- <script type=\"module\"> 被当作入口（会 bundle / 注入 ?hash）
- 普通 <script src=\"...\"> 留作原样引用，路径对应 publicDir 下的文件
- publicDir 设的是 frontend/static/public，文件 vendor/echarts.min.js → URL /vendor/echarts.min.js
"""
from pathlib import Path

p = Path("frontend/index.html")
src = p.read_text(encoding="utf-8")

old = '''  <!-- 单入口 ESM：app.js 内部 import 所有依赖模块；main.py 注入 ?v= 强刷 -->
  <script type="module" src="/static/js/app.js"></script>'''

new = '''  <!-- 普通脚本：echarts 作为 window.echarts 全局（Vite 从 publicDir 拷贝原样） -->
  <script src="/vendor/echarts.min.js"></script>
  <!-- 单入口 ESM：app.js 内部 import 所有依赖模块；main.py 注入 ?v= 强刷 -->
  <script type="module" src="/static/js/app.js"></script>'''

if old not in src:
    raise SystemExit("未找到 ESM 入口行")
src = src.replace(old, new, 1)
p.write_text(src, encoding="utf-8")
print("OK")
