"""Dry-run app.js 看 DOMContentLoaded 处理。"""
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
from _esm_transform import transform, ROOT  # noqa: E402

for f in ["app.js", "charts.js", "settings.js", "ai.js", "page-home.js"]:
    text = (ROOT / f).read_text(encoding="utf-8")
    new = transform(f, text)
    out = Path(f"scripts/_dry_{f.replace('.js','')}.js")
    out.write_text(new, encoding="utf-8")
    print(f"\n===== {f} =====")
    lines = new.splitlines()
    print(f"  total {len(lines)} lines")
    print("  HEAD 8:")
    for ln in lines[:8]:
        print(f"    {ln}")
    print("  TAIL 6:")
    for ln in lines[-6:]:
        print(f"    {ln}")
