"""修正 _esm_transform.py 中 export 替换的 bug：使用 re.sub 替代手动切片。"""
from pathlib import Path

p = Path("scripts/_esm_transform.py")
src = p.read_text(encoding="utf-8")

old = """    name_target = meta["name"]
    export_pat = re.compile(rf"^\\s*global\\.{re.escape(name_target)}\\s*=\\s*\\{{", re.MULTILINE)
    body_text = "".join(body)
    m = export_pat.search(body_text)
    if not m:
        raise RuntimeError(f"{name}: 未找到 export 起点 `global.{name_target} = {{`")
    # 把 `global.X = {` 替换为 `export const X = {`
    body_text = body_text[:m.start()] + f"export const {name_target} = " + body_text[m.start() + len(f"global.{name_target} = "):]"""

new = """    name_target = meta["name"]
    export_pat = re.compile(rf"^(\\s*)global\\.{re.escape(name_target)}(\\s*=\\s*\\{{)", re.MULTILINE)
    body_text = "".join(body)
    if not export_pat.search(body_text):
        raise RuntimeError(f"{name}: 未找到 export 起点 `global.{name_target} = {{`")
    # 用 re.sub 整体替换（保留前导缩进与赋值符号），避免手动切片算错偏移
    body_text = export_pat.sub(rf"\\1export const {name_target}\\2", body_text, count=1)"""

if old not in src:
    raise SystemExit(f"未找到 old 块，len(src)={len(src)}")
src = src.replace(old, new, 1)
p.write_text(src, encoding="utf-8")
print("OK")
