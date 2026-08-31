"""修 tests/test_utils.py::test_safe_url：util.js 已从 IIFE+global 转 ESM，
原本 `require(util.js); U=global.window.U` 不再有效，改为 dynamic import。
"""
from pathlib import Path

p = Path("tests/test_utils.py")
src = p.read_text(encoding="utf-8")

old = '''    util = (root / "frontend" / "static" / "js" / "util.js").as_posix()
    script = (
        "global.window={};global.document={};require('%s');"
        "const U=global.window.U;"
        "const pass=['https://a.cn/x','http://a.cn/x','//a.cn/x','/api/x'];"
        "const block=['javascript:alert(1)','JavaScript:alert(1)','java\\tscript:alert(1)',"
        "' javascript:alert(1)','data:text/html,<script>a</script>','vbscript:msgbox(1)'];"
        "let bad=[];"
        "for(const u of pass){if(U.safeUrl(u)!==u)bad.push('应放行:'+u);}"
        "for(const u of block){if(U.safeUrl(u)!=='')bad.push('应拦截:'+u);}"
        "console.log(bad.length?bad.join('|'):'OK');"
    ) % util
    try:
        proc = subprocess.run(
            [node, "-e", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )'''

new = '''    util = (root / "frontend" / "static" / "js" / "util.js").as_posix()
    # util.js 已从 IIFE+global 转 ESM，原 require + global.U 不再可用；改用 dynamic import
    # 加 --input-type=module 让 Node 把 -e 字符串按 ESM 解析（顶层 await import 才合法）
    script = (
        "global.window={};global.document={};"
        "(async()=>{"
        "try{"
        "const{U}=await import('%s');"
        "const pass=['https://a.cn/x','http://a.cn/x','//a.cn/x','/api/x'];"
        "const block=['javascript:alert(1)','JavaScript:alert(1)','java\\\\tscript:alert(1)',"
        "' javascript:alert(1)','data:text/html,<script>a</script>','vbscript:msgbox(1)'];"
        "let bad=[];"
        "for(const u of pass){if(U.safeUrl(u)!==u)bad.push('应放行:'+u);}"
        "for(const u of block){if(U.safeUrl(u)!=='')bad.push('应拦截:'+u);}"
        "console.log(bad.length?bad.join('|'):'OK');"
        "}catch(e){console.log('ERR:'+e.message);}"
        "})();"
    ) % util
    try:
        proc = subprocess.run(
            [node, "--input-type=module", "-e", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )'''

if old not in src:
    raise SystemExit("未找到待替换的 test_safe_url 块")
src = src.replace(old, new, 1)
p.write_text(src, encoding="utf-8")
print("OK")
