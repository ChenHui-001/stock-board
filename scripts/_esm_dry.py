"""Dry-run: 只转换 util.js，写到 /tmp/util.esm.js 用于人工核对。"""
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from _esm_transform import transform, ROOT  # noqa: E402

text = (ROOT / "util.js").read_text(encoding="utf-8")
new = transform("util.js", text)
out = Path("scripts/_dry_util.js")
out.write_text(new, encoding="utf-8")
print(f"Wrote {out} ({len(new)} bytes)")
print("\n=== 前 30 行 ===")
for ln in new.splitlines()[:30]:
    print(ln)
print("\n=== 末 30 行 ===")
for ln in new.splitlines()[-30:]:
    print(ln)
