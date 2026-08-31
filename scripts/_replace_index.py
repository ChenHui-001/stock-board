"""把 frontend/index.html 的 13 个 <script> 标签替换成单入口 ESM。"""
from pathlib import Path
import re

p = Path("frontend/index.html")
src = p.read_text(encoding="utf-8")

# 整段匹配：从 echarts.min.js 到 app.js
old = '''  <script src="/static/vendor/echarts.min.js"></script>
  <script src="/static/js/util.js"></script>
  <script src="/static/js/api.js"></script>
  <script src="/static/js/charts.js"></script>
  <script src="/static/js/page-home.js"></script>
  <script src="/static/js/page-search.js"></script>
  <script src="/static/js/page-hotspot.js"></script>
  <script src="/static/js/page-value.js"></script>
  <script src="/static/js/page-detail.js"></script>
  <script src="/static/js/ai.js"></script>
  <script src="/static/js/news.js"></script>
  <script src="/static/js/settings.js"></script>
  <script src="/static/js/app.js"></script>'''

new = '''  <!-- 单入口 ESM：app.js 内部 import 所有依赖模块；main.py 注入 ?v= 强刷 -->
  <script type="module" src="/static/js/app.js"></script>'''

if old not in src:
    raise SystemExit(f"未找到目标块，src 前 200 字符: {src[:200]!r}")
src = src.replace(old, new, 1)
p.write_text(src, encoding="utf-8")
print("OK 已替换 13 个 <script> 为单入口 ESM")
