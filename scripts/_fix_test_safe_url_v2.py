"""直接定位并替换 test_safe_url 里的 require + global.U，改 dynamic import。"""
from pathlib import Path
import re

p = Path("tests/test_utils.py")
src = p.read_text(encoding="utf-8")

# 找到 'global.window={};global.document={};require(' 起点
# 和 ']) % util' 终点
start_marker = "global.window={};global.document={};require("
end_marker = "]) % util"

i = src.find(start_marker)
j = src.find(end_marker)
if i == -1 or j == -1:
    raise SystemExit("未找到锚点")
i += len(start_marker)  # 移到 require( 之后
# j 是 end_marker 的位置，end_marker 之前是 ']) '，所以切片到 j + 3 (']) ')
old_block = src[i:j + len(end_marker)]

# 构造新代码块（dynamic import + IIFE + try/catch）
# 先把 util.js 路径放进去
new_template = '''file://{util_path}');
        "const pass=['https://a.cn/x','http://a.cn/x','//a.cn/x','/api/x'];"
        "const block=['javascript:alert(1)','JavaScript:alert(1)','java\\\\tscript:alert(1)',"
        "' javascript:alert(1)','data:text/html,<script>a</script>','vbscript:msgbox(1)'];"
        "let bad=[];"
        "for(const u of pass){if(U.safeUrl(u)!==u)bad.push('PASS:'+u);}"
        "for(const u of block){if(U.safeUrl(u)!=='')bad.push('BLOCK:'+u);}"
        "console.log(bad.length?bad.join('|'):'OK');"
        "}catch(e){console.log('ERR:'+e.message);}"
        "})();"'''
new_block = new_template.format(util_path="{util}")

# 替换：去旧块，新块占位 {util} 之后用 % util 替换
# 实际实现：把 old_block 替换成 new_block（保留 % util 在后面）
# 但 new_template 里已经有 ']) ' 结构了，需要重新设计

# 更简单：直接把整个 "global.window={};..." + "]) % util" 区间替换
# 找到 src[i:j+len(']) % util')] 整体替换为新代码（不含 % util）
prefix_to_replace = src[i:j]
new_prefix = '''file://{util_path}');
        "const pass=['https://a.cn/x','http://a.cn/x','//a.cn/x','/api/x'];"
        "const block=['javascript:alert(1)','JavaScript:alert(1)','java\\\\tscript:alert(1)',"
        "' javascript:alert(1)','data:text/html,<script>a</script>','vbscript:msgbox(1)'];"
        "let bad=[];"
        "for(const u of pass){if(U.safeUrl(u)!==u)bad.push('PASS:'+u);}"
        "for(const u of block){if(U.safeUrl(u)!=='')bad.push('BLOCK:'+u);}"
        "console.log(bad.length?bad.join('|'):'OK');"
        "}catch(e){console.log('ERR:'+e.message);}"
        "})();"'''

new_prefix = new_prefix.replace('{util_path}', '{util}')

# old prefix (between `require(` and `])`):
# 内容：'%s');"const pass=...const block=...
# 我们要把 `require('%s');` → `file://{util}');` + (async()=>{try{const{U}=await import(' + '})();
# 同时包成 IIFE + async 让 dynamic import 顶层 await 合法

# 重写整段：替换 src[i:j+len(end_marker)] 为新结构
# 新结构（在 -e 字符串里）：
old_section = src[i:j + len(end_marker)]  # 包含 "]) % util"
new_section = '{util}'
src = src.replace(old_section, new_section)

# 现在拼装新的 -e 字符串内容
# 找到 require( 之前的 "global.window={};global.document={};"，保留它
# 把 require( ... ) % util 替换为 dynamic import + IIFE
# 但因为我们刚才已经替换了旧字符串，现在需要把模板填进 src

# 找到占位符 '{util}' 位置，替换为完整 dynamic import 代码（带 util 占位）
placeholder = '{util}'
i2 = src.find(placeholder)
if i2 == -1:
    raise SystemExit("占位符 {util} 未找到")

# 在占位符位置插入完整 dynamic import 模板
new_template_str = """file://' + util + '');
        "const pass=['https://a.cn/x','http://a.cn/x','//a.cn/x','/api/x'];"
        "const block=['javascript:alert(1)','JavaScript:alert(1)','java\\\\tscript:alert(1)',"
        "' javascript:alert(1)','data:text/html,<script>a</script>','vbscript:msgbox(1)'];"
        "let bad=[];"
        "for(const u of pass){if(U.safeUrl(u)!==u)bad.push('PASS:'+u);}"
        "for(const u of block){if(U.safeUrl(u)!=='')bad.push('BLOCK:'+u);}"
        "console.log(bad.length?bad.join('|'):'OK');"
        "}catch(e){console.log('ERR:'+e.message);}"
        "})();""""

# 简化：在脚本模板前加 async wrapper + import
# 实际上重写 test_safe_url 整个函数块更稳

# 简化路径：直接重写整个 test_safe_url 函数
func_start = src.find("def test_safe_url() -> None:")
func_end_marker = 'assert (out == "OK"), out or (proc.stderr or "")[:200]'
func_end = src.find(func_end_marker) + len(func_end_marker)
if func_start == -1 or func_end < len(func_end_marker):
    raise SystemExit("未找到函数边界")

new_func = '''def test_safe_url() -> None:
    """验证前端 safeUrl 的安全过滤行为。

    util.js 已从 IIFE+global 转 ESM（阶段 4），原 `require + global.U` 不再可用，
    改为 `--input-type=module + dynamic import`。
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        print("[skip] 前端 safeUrl（未安装 node）")
        return

    root = Path(__file__).resolve().parent.parent
    util = (root / "frontend" / "static" / "js" / "util.js").as_posix()
    script = (
        "global.window={};global.document={};"
        "(async()=>{try{"
        "const{U}=await import('file://' + '{util}');"
        "const pass=['https://a.cn/x','http://a.cn/x','//a.cn/x','/api/x'];"
        "const block=['javascript:alert(1)','JavaScript:alert(1)','java\\\\tscript:alert(1)',"
        "' javascript:alert(1)','data:text/html,<script>a</script>','vbscript:msgbox(1)'];"
        "let bad=[];"
        "for(const u of pass){if(U.safeUrl(u)!==u)bad.push('PASS:'+u);}"
        "for(const u of block){if(U.safeUrl(u)!=='')bad.push('BLOCK:'+u);}"
        "console.log(bad.length?bad.join('|'):'OK');"
        "}catch(e){console.log('ERR:'+e.message);}"
        "})();"
    ).format(util=util)
    try:
        proc = subprocess.run(
            [node, "--input-type=module", "-e", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        assert (False), f"超时: {exc}"
        return
    out = (proc.stdout or "").strip()
    assert (out == "OK"), out or (proc.stderr or "")[:200]'''

# 用占位符 {util} 重写
old_section_2 = src[func_start:func_end]
src = src.replace(old_section_2, new_func)

p.write_text(src, encoding="utf-8")
print("OK 已重写 test_safe_url")
