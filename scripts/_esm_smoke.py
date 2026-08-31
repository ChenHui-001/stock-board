"""用 Node 把 app.js 当 ESM 加载，只导入不执行 DOM 代码，看 module graph 能否解析。"""
import json
import subprocess
import sys
from pathlib import Path

# 写一个简单的 .mjs 入口，通过 Node 把 app.js 当 entry 导入
# 但 app.js 会调用 DOMContentLoaded / window 等浏览器 API，会崩。
# 所以只导入直到 顶层 import 都能解析：
#   import('./app.js') 会拉取所有依赖、跑顶层副作用
#   若顶层副作用崩（如 window.addEventListener），会报错

# 方案：写一个 fake window/document global stub，然后 dynamic import app.js
stub = r"""
// Stub browser globals
globalThis.window = {
  addEventListener: () => {},
  innerWidth: 1920,
  innerHeight: 1080,
  scrollTo: () => {},
  scrollY: 0,
  isSecureContext: false,
};
globalThis.document = {
  addEventListener: (ev, cb) => { if (ev === 'DOMContentLoaded') setTimeout(cb, 0); },
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => ({ style: {}, appendChild: () => {}, addEventListener: () => {} }),
  body: { appendChild: () => {} },
  getElementById: () => null,
  readyState: 'complete',
};
globalThis.fetch = async () => ({ ok: true, text: async () => '{}' });
globalThis.AbortController = class { constructor() { this.signal = {}; } abort() {} };
globalThis.setTimeout = setTimeout;
globalThis.clearTimeout = clearTimeout;

// ECharts global stub
globalThis.echarts = { init: () => ({ setOption: () => {}, resize: () => {}, dispose: () => {} }) };

// 防止 navigate/sessionStorage 等
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node-test' }, configurable: true });
Object.defineProperty(globalThis, 'location', { value: { hash: '', reload: () => {} }, configurable: true });

import { pathToFileURL } from 'node:url';
const appPath = pathToFileURL('E:/project/股票看板/frontend/static/js/app.js').href;
const mod = await import(appPath);
console.log('imports OK, exports:', Object.keys(mod));
"""

tmp = Path("tests/_smoke_data/_node_import.mjs")
tmp.write_text(stub, encoding="utf-8")

r = subprocess.run(
    ["node", str(tmp)],
    capture_output=True, text=True, timeout=30,
)
print("STDOUT:", r.stdout.strip())
print("STDERR:", r.stderr.strip()[:2000])
print("EXIT:", r.returncode)
