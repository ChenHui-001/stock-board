"""改 Node 语法检查：把每个 .js 复制到临时 .mjs 再 --check，避开 file 路径 + --input-type 冲突。"""
from pathlib import Path

p = Path("scripts/_verify_esm.py")
src = p.read_text(encoding="utf-8")

old = '''r = subprocess.run(
        ["node", "--check", "--input-type=module", str(path)],
        capture_output=True, text=True,
    )'''

new = '''import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = tmp.name
    try:
        r = subprocess.run(
            ["node", "--check", tmp_path],
            capture_output=True, text=True,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)'''

if old not in src:
    raise SystemExit("未找到 old 块")
src = src.replace(old, new, 1)
p.write_text(src, encoding="utf-8")
print("OK")
