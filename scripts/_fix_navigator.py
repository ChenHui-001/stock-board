"""修 ESM smoke stub：移除只读 navigator 赋值。"""
from pathlib import Path

p = Path("scripts/_esm_smoke.py")
src = p.read_text(encoding="utf-8")
src = src.replace(
    "globalThis.navigator = { userAgent: 'node-test' };",
    "Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node-test' }, configurable: true });",
)
src = src.replace(
    "globalThis.location = { hash: '', reload: () => {} };",
    "Object.defineProperty(globalThis, 'location', { value: { hash: '', reload: () => {} }, configurable: true });",
)
p.write_text(src, encoding="utf-8")
print("OK")
