from pathlib import Path

p = Path("tests/test_utils.py")
text = p.read_text(encoding="utf-8")
func_start = text.find("def test_safe_url() -> None:")
i = func_start + 1
while i < len(text):
    if text[i:].startswith("\ndef ") or text[i:].startswith("\nclass "):
        func_end = i + 1
        break
    i += 1
else:
    func_end = len(text)

new_func = """def test_safe_url() -> None:
    \"\"\"验证前端 safeUrl 的安全过滤行为。

    util.js 已从 IIFE+global 转 ESM（阶段 4），原 require + global.U 不再可用，
    改为 --input-type=module + dynamic import。
    \"\"\"
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        print("[skip] 前端 safeUrl（未安装 node）")
        return

    root = Path(__file__).resolve().parent.parent
    util = (root / "frontend" / "static" / "js" / "util.js").as_posix()
    # 用 __UTIL_PATH__ 占位避免与 JS 的 {} 冲突；用 replace 替换（不用 format）
    script = (
        "global.window={};global.document={};"
        "(async()=>{try{"
        "const{U}=await import('file://' + '__UTIL_PATH__');"
        "const pass=['https://a.cn/x','http://a.cn/x','//a.cn/x','/api/x'];"
        "const block=['javascript:alert(1)','JavaScript:alert(1)','java\\\\tscript:alert(1)',"
        "' javascript:alert(1)','data:text/html,<script>a</script>','vbscript:msgbox(1)'];"
        "let bad=[];"
        "for(const u of pass){if(U.safeUrl(u)!==u)bad.push('PASS:'+u);}"
        "for(const u of block){if(U.safeUrl(u)!=='')bad.push('BLOCK:'+u);}"
        "console.log(bad.length?bad.join('|'):'OK');"
        "}catch(e){console.log('ERR:'+e.message);}"
        "})();"
    ).replace("__UTIL_PATH__", util)
    try:
        proc = subprocess.run(
            [node, "--input-type=module", "-e", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        assert (False), f"超时: {exc}"
        return
    out = (proc.stdout or "").strip()
    assert (out == "OK"), out or (proc.stderr or "")[:200]
"""

new_text = text[:func_start] + new_func + text[func_end:]
p.write_text(new_text, encoding="utf-8")
print(f"OK new func written: {len(new_func)} chars")
