"""Utils。"""
from __future__ import annotations

from tests._common import *  # noqa: F401,F403  公共导入见 tests/_common.py
from backend.indicators import build_ma, summarize_flow, support_resistance
from backend.providers import registry
from backend.providers.base import Bar, FlowDay

def test_describe_exc() -> None:
    import httpx

    from backend.utils import describe_exc

    empty = [httpx.ReadTimeout(""), httpx.ConnectTimeout(""), httpx.PoolTimeout(""),
             httpx.WriteTimeout(""), httpx.ReadError("")]
    for exc in empty:
        got = describe_exc(exc)
        assert (got == type(exc).__name__), f"got={got!r}"
    assert (describe_exc(httpx.ConnectError("getaddrinfo failed")) == "getaddrinfo failed")

    # LLM 路径：超时应给出可执行提示，且不得出现空的尾巴
    msg = llm._request_error(httpx.ReadTimeout(""), "https://api.deepseek.com/v1/chat/completions")
    assert ("api.deepseek.com" in msg), msg
    assert (len(msg.strip()) > 10), msg





def test_items_fingerprint() -> None:
    """解读缓存指纹：条目内容一变指纹即变，防止新标题配旧解读的缓存错位。"""
    from backend.utils import items_fingerprint

    a = [{"id": "1", "date": "2026-08-18", "title": "中标合同"},
         {"id": "2", "date": "2026-08-17", "title": "回购"}]
    b = [{"id": "2", "date": "2026-08-17", "title": "回购"},
         {"id": "3", "date": "2026-08-16", "title": "减持"}]
    assert (items_fingerprint(a) == items_fingerprint(list(a))), items_fingerprint(a)
    assert (items_fingerprint(a) != items_fingerprint(b)), f"{items_fingerprint(a)} vs {items_fingerprint(b)}"
    assert (items_fingerprint([{"id": "1", "title": "A"}]) != items_fingerprint([{"id": "1", "title": "B"}]))
    collision_a = [{"id": "1", "date": "2026:08", "title": "18"}]
    collision_b = [{"id": "1", "date": "2026", "title": "08:18"}]
    assert (items_fingerprint(collision_a) != items_fingerprint(collision_b))
    assert (isinstance(items_fingerprint([]), str) and len(items_fingerprint([])) == 12)





def test_safe_url() -> None:
    """验证前端 safeUrl 的安全过滤行为。

    util.js 已从 IIFE+global 转 ESM（阶段 4），原 require + global.U 不再可用，
    改为 --input-type=module + dynamic import。
    """
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
        "const block=['javascript:alert(1)','JavaScript:alert(1)','java\\tscript:alert(1)',"
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
