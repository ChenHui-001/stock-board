"""Cache。"""
from __future__ import annotations

from tests._common import *  # noqa: F401,F403  公共导入见 tests/_common.py
from backend.indicators import build_ma, summarize_flow, support_resistance
from backend.providers import registry
from backend.providers.base import Bar, FlowDay

def test_cache() -> None:
    async def run() -> None:
        c = cache.TTLCache()
        calls = {"n": 0}

        async def loader() -> str:
            calls["n"] += 1
            await asyncio.sleep(0.01)
            return "v"

        await asyncio.gather(*[c.get_or_set("k", 60, loader) for _ in range(5)])
        assert (calls["n"] == 1), f"n={calls['n']}"
        assert (not c._locks)
        # 过期后应重新加载
        c._data["k"] = (0.0, "stale")
        await c.get_or_set("k", 60, loader)
        assert (calls["n"] == 2), f"n={calls['n']}"
        # 上限淘汰后不超限
        for i in range(cache.MAX_ENTRIES + 50):
            c.put(f"bulk{i}", i, 100.0)
        assert (len(c._data) <= cache.MAX_ENTRIES), f"n={len(c._data)}"

    asyncio.run(run())





def test_ai_lock() -> None:
    async def run() -> None:
        calls = {"n": 0}
        store: dict[str, str] = {}

        async def work() -> str:
            if "report" in store:          # 等锁后二次检查缓存
                return store["report"]
            calls["n"] += 1                 # 真实计算只应执行一次
            await asyncio.sleep(0.01)
            store["report"] = "done"
            return store["report"]

        r = await asyncio.gather(*[api._with_ai_lock("600000", work) for _ in range(5)])
        assert (calls["n"] == 1 and all(x == "done" for x in r)), f"n={calls['n']}"
        assert ("600000" not in api._ai_locks)

    asyncio.run(run())





def test_ai_cache_freshness() -> None:
    """AI 当日缓存时效：过期快照必须作废重建（保证点击分析时是最新实时数据）。"""
    from datetime import datetime, timedelta

    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    fresh = api._cache_fresh((now - timedelta(seconds=30)).strftime(fmt))
    assert (fresh is True), f"fresh={fresh}"
    # 2 小时前快照超过盘后 1h TTL，任何时段（盘中 120s / 盘后 1h）都必过期
    stale = api._cache_fresh((now - timedelta(hours=2)).strftime(fmt))
    assert (stale is False), f"stale={stale}"
    bad = api._cache_fresh("not-a-date")
    assert (bad is False), f"bad={bad}"
    # 字段缺失（get_report 无 cached_at）同样视为过期，宁可重建
    none_at = api._cache_fresh("")
    assert (none_at is False), f"none={none_at}"





def test_ai_cache_blank_degraded_invalidated() -> None:
    """历史缺陷缓存作废：旧版本把空白「LLM 请求失败: 」写进 degraded_reason，

    升级后这些缓存仍能通过各项校验继续命中，用户会一直看到空白报错；
    检测到即作废重建，有具体内容的降级原因则正常命中。
    """
    from backend import scorecfg

    base: dict[str, object] = {
        "code": "600000", "name": "测试", "board": "",
        "price": 10.0, "change_pct": 0.0,
        "analysis": {"advice": {"scores": {"intraday": 1}}},
        "meta": {
            "engine": "rule", "model": "内置规则引擎",
            "generated_at": "2026-08-21 10:00:00",
            "fingerprint": llmcfg.fingerprint(),
            "score_fp": scorecfg.fingerprint(),
            "schema_version": api.REPORT_SCHEMA_VERSION,
            "degraded_reason": "AI 服务调用失败（LLM 请求失败: ），已降级为内置规则引擎",
        },
        "report_sentiment": {"bull": 0, "bear": 0, "neutral": 0},
        "rating_dist": {}, "reports_preview": [], "status_tags": [],
    }
    storage.save_report("600000", base)
    assert (api._cached_report("600000") is None)

    # 有具体内容的降级原因（如当前版本的超时提示）应正常命中
    base["meta"]["degraded_reason"] = (  # type: ignore[index]
        "AI 服务调用失败（LLM 请求失败: 等待 api.deepseek.com 响应超时（90s）。"
        "请调大 LLM_TIMEOUT 环境变量，或换用更快的模型），已降级为内置规则引擎"
    )
    storage.save_report("600000", base)
    hit = api._cached_report("600000")
    assert (hit is not None and hit.get("from_cache") is True), f"hit={hit is not None}"

    # 正则本身：空白尾巴匹配，有内容不匹配
    blank = api._BLANK_LLM_REASON_RE.search("AI 服务调用失败（LLM 请求失败: ），已降级为内置规则引擎")
    assert (blank is not None)
    ok_reason = "AI 服务调用失败（LLM 请求失败: 无法连接 api.deepseek.com，请检查网络）"
    assert (api._BLANK_LLM_REASON_RE.search(ok_reason) is None)




def test_negative_cache() -> None:
    """loader 返回 None 应短 TTL 负缓存（防穿透）；force / 过期 / negative_ttl=0 时正常重试。"""
    async def run() -> None:
        c = cache.TTLCache()
        calls = {"n": 0}

        async def empty() -> None:
            calls["n"] += 1
            return None

        # 第一次 miss → loader；第二次命中负缓存 → loader 不再被调
        assert await c.get_or_set("nk", 60, empty) is None
        assert await c.get_or_set("nk", 60, empty) is None
        assert calls["n"] == 1, f"n={calls['n']}"
        # 外部 peek 对负缓存哨兵不可见
        assert c.peek("nk") is None
        # force 绕过负缓存
        assert await c.get_or_set("nk", 60, empty, force=True) is None
        assert calls["n"] == 2, f"n={calls['n']}"
        # negative_ttl=0 关闭负缓存 → 每次都穿透
        assert await c.get_or_set("nk2", 60, empty, negative_ttl=0) is None
        assert await c.get_or_set("nk2", 60, empty, negative_ttl=0) is None
        assert calls["n"] == 4, f"n={calls['n']}"
        # 负缓存过期后恢复 loader 调用
        c._data["nk"] = (0.0, cache._NEG)
        await c.get_or_set("nk", 60, empty)
        assert calls["n"] == 5, f"n={calls['n']}"

    asyncio.run(run())
