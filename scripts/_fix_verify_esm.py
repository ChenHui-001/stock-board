"""修验证脚本：(1) regex 允许前导空格；(2) Node --check 用 .mjs 重命名或加 --input-type=module。"""
from pathlib import Path

p = Path("scripts/_verify_esm.py")
src = p.read_text(encoding="utf-8")

# 1. 修 regex：允许前导空格 / Tab
src = src.replace(
    'has_import = bool(re.search(r"^import\\s+", text, re.MULTILINE))',
    'has_import = bool(re.search(r"^[ \\t]*import\\s+", text, re.MULTILINE))',
)
src = src.replace(
    'has_export = bool(re.search(r"^export\\s+", text, re.MULTILINE))',
    'has_export = bool(re.search(r"^[ \\t]*export\\s+", text, re.MULTILINE))',
)

# 2. Node --check 用 --input-type=module，让 Node 把 stdin 当 ESM 解析
src = src.replace(
    'r = subprocess.run(\n        ["node", "--check", str(path)],\n        capture_output=True, text=True,\n    )',
    'r = subprocess.run(\n        ["node", "--check", "--input-type=module", str(path)],\n        capture_output=True, text=True,\n    )',
)

p.write_text(src, encoding="utf-8")
print("OK")
