"""阶段 4.1 — 把 frontend/static/js/*.js 从 IIFE+global 转成 ESM。

策略：
- 11 个页面 / 工具文件都是 `(function (global) { ... global.X = {...}; })(window)`
  标准模式；3 个文件 (charts.js/app.js/settings.js) 在尾部有"副作用语句"
  (window.addEventListener / document.addEventLoaded)。
- 转换规则（保持代码语义不变）：
  1. 删 `(function (global) {` 和 `})(window);`
  2. 删 `'use strict';`（ESM 默严格）
  3. `global.X = {...}` → `export const X = {...}`
  4. `global.addEventListener` 之类的"window 引用" → `window.addEventListener`
  5. `document.addEventListener('DOMContentLoaded', ...)` → 提到模块顶层，
     且保留原 listener（ESM defer 语义下 DOMContentLoaded 已触发 → 改为同步调用）
  6. 顶部加 `import { ... } from './x.js'`，依据 deps 表

- 注意：app.js / settings.js 里的 DOMContentLoaded listener 在 ESM 里不会触发；
  转换时把 callback 同步提到模块尾部。charts.js 的 resize listener 保持原样
  （window 始终存在，addEventListener 注册即生效）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path("frontend/static/js")

# 每个文件的"导出名"和"依赖"
# name: 导出标识符；file: import 源文件；deps: 自身依赖列表（按 file -> name）
META = {
    "util.js":         {"name": "U",          "deps": []},
    "api.js":          {"name": "API",        "deps": []},
    "charts.js":       {"name": "Charts",     "deps": [("util.js", "U")]},
    "ai.js":           {"name": "AI",         "deps": [("util.js", "U"), ("api.js", "API")]},
    "news.js":         {"name": "News",       "deps": [("util.js", "U"), ("api.js", "API"), ("ai.js", "AI")]},
    "page-search.js":  {"name": "PageSearch", "deps": [("util.js", "U"), ("api.js", "API")]},
    "page-value.js":   {"name": "PageValue",  "deps": [("util.js", "U"), ("api.js", "API")]},
    "page-hotspot.js": {"name": "PageHotspot","deps": [("util.js", "U"), ("api.js", "API"), ("ai.js", "AI")]},
    "page-home.js":    {"name": "PageHome",   "deps": [("util.js", "U"), ("api.js", "API"), ("app.js", "App"), ("ai.js", "AI"), ("news.js", "News")]},
    "page-detail.js":  {"name": "PageDetail", "deps": [("util.js", "U"), ("api.js", "API"), ("app.js", "App"), ("charts.js", "Charts"), ("ai.js", "AI")]},
    "settings.js":     {"name": "Settings",   "deps": [("util.js", "U"), ("api.js", "API"), ("app.js", "App"), ("ai.js", "AI")]},
    "app.js":          {"name": "App",        "deps": [("util.js", "U"), ("api.js", "API"), ("charts.js", "Charts"), ("ai.js", "AI"),
                                                       ("page-home.js", "PageHome"), ("page-search.js", "PageSearch"),
                                                       ("page-hotspot.js", "PageHotspot"), ("page-value.js", "PageValue"),
                                                       ("page-detail.js", "PageDetail"), ("settings.js", "Settings")]},
}

# 副作用文件：DOMContentLoaded 监听器需要改成同步调用（ESM defer 后事件已触发）
# 提取需要同步执行的语句（App.start / Settings.bind）
DOM_READY_FILES = {
    "app.js":      "App.start();",
    "settings.js": "Settings.bind();",
}

# resize 监听保持原样（window.addEventListener 不会受 ESM defer 影响）


def transform(name: str, text: str) -> str:
    meta = META[name]
    lines = text.splitlines(keepends=True)

    # 1. 找 IIFE 起始和结束（行索引，0-based）
    iife_start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("(function (global)") or s.startswith("(function(global)"):
            iife_start = i
            break
    if iife_start is None:
        raise RuntimeError(f"{name}: 未找到 IIFE 起始")

    iife_end = None
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if s.startswith("})(window);") or s.startswith("})(global);"):
            iife_end = i
            break
    if iife_end is None:
        raise RuntimeError(f"{name}: 未找到 IIFE 结束")

    # 2. 切出 IIFE body（不包括起始和结束行）
    body = lines[iife_start + 1 : iife_end]
    # 移除 'use strict';
    body = [ln for ln in body if ln.strip() != "'use strict';"]

    # 3. 处理 DOMContentLoaded 文件：把 callback 提到模块尾部同步执行
    if name in DOM_READY_FILES:
        sync_call = DOM_READY_FILES[name]
        new_body = []
        kept_dom_listener = False
        for ln in body:
            stripped = ln.strip()
            # document.addEventListener('DOMContentLoaded', function () { XXX.start(); });
            m = re.match(r"document\.addEventListener\(\s*['\"]DOMContentLoaded['\"]\s*,\s*function\s*\([^)]*\)\s*\{\s*(\w+)\.(\w+)\([^)]*\);\s*\}", stripped)
            if m:
                # 把原 listener 替换为同步调用
                new_body.append(f"// ESM defer 下 DOMContentLoaded 已触发 → 改同步执行\n{sync_call}\n")
                kept_dom_listener = True
            else:
                new_body.append(ln)
        body = new_body

    # 4. 处理 export 转换：找到 `global.X = { ... };` 这一段（最后赋值）
    name_target = meta["name"]
    export_pat = re.compile(rf"^(\s*)global\.{re.escape(name_target)}(\s*=\s*\{{)", re.MULTILINE)
    body_text = "".join(body)
    if not export_pat.search(body_text):
        raise RuntimeError(f"{name}: 未找到 export 起点 `global.{name_target} = {{`")
    # 用 re.sub 整体替换（保留前导缩进与赋值符号），避免手动切片算错偏移
    body_text = export_pat.sub(rf"\1export const {name_target}\2", body_text, count=1)

    # 5. 其它 `global.X` 引用（如 `global.addEventListener`）→ `window.X`
    body_text = re.sub(r"\bglobal\.", "window.", body_text)

    # 6. 顶部加 import 块（按依赖顺序：先 util，再 api，再 ai，再 news，再 page，最后 app）
    deps_sorted = sorted(meta["deps"], key=lambda d: list(META.keys()).index(d[0]))
    if deps_sorted:
        # 按文件聚合，多个名字同一文件合并到一个 import
        from collections import OrderedDict
        grouped: "OrderedDict[str, list[str]]" = OrderedDict()
        for fname, sym in deps_sorted:
            grouped.setdefault(fname, []).append(sym)
        import_lines = []
        for fname, syms in grouped.items():
            import_lines.append(f"import {{ {', '.join(syms)} }} from './{fname}';")
        import_block = "\n".join(import_lines) + "\n\n"
    else:
        import_block = ""

    # 7. 顶部加注释：原 IIFE 描述保留
    header_comment = f"/* {Path(name).stem}（从 IIFE+global 转 ESM） */\n"

    return header_comment + import_block + body_text


def main():
    changed = []
    for name, meta in META.items():
        src = (ROOT / name).read_text(encoding="utf-8")
        new = transform(name, src)
        (ROOT / name).write_text(new, encoding="utf-8")
        changed.append((name, len(src), len(new)))
    print("转换完成：")
    for name, before, after in changed:
        print(f"  {name:18}: {before:>6} → {after:>6} 字节 (Δ {after - before:+d})")


if __name__ == "__main__":
    main()
