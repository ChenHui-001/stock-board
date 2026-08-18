"""离线冒烟测试：CI 门禁用，不依赖外部网络 / 数据源 / 浏览器。

覆盖：模块可导入、LLM JSON 修复阶梯、LLM 配置指纹、缓存单飞与过期重载、
AI 每股票单飞锁、均线与支撑压力指标、数据源注册表装配。
运行：python backend/smoke_test.py（退出码 0=通过）
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# 用临时数据目录，避免污染工作区 / CI 环境
_tmp = tempfile.mkdtemp(prefix="board-smoke-")
os.environ["DATA_DIR"] = _tmp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import analysis, api, cache, llm, llmcfg, news, storage  # noqa: E402
from backend.indicators import build_ma, support_resistance  # noqa: E402
from backend.providers import registry  # noqa: E402
from backend.providers.base import Bar  # noqa: E402

FAILED: list[str] = []
TOTAL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"[{'ok' if cond else 'FAIL'}] {name}" + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ------------------------------------------------------------------ LLM JSON 修复阶梯
def test_json_repair() -> None:
    cases: dict[str, str] = {
        '{"action": "持有观望", "confidence": 80}': "持有观望",
        '```json\n{"action": "积极持仓/加仓"}\n```': "积极持仓/加仓",
        '```JSON\n{"action": "清仓离场"}\n```': "清仓离场",
        "前文寒暄 {\"action\": \"减仓规避\"} 结尾废话": "减仓规避",
        '{"action": "清仓离场", "list": [1, 2,]}': "清仓离场",
        '{"action": "持有观望", "trend": {"summary": "多头", "short": "截断': "持有观望",
        '{"advice": {"action": "持有观望", "reason": "依据", "support": 12.34': "持有观望",
        '{"action": "减仓规避", "a": {"b": [1,2,3], "c": {"d": 1}': "减仓规避",
        '{"action": "持有观望", "x": 1}{"action": "减仓规避"}': "持有观望",
    }
    for raw, want in cases.items():
        try:
            obj = llm._extract_json(raw)
            got = obj.get("action") or (obj.get("advice") or {}).get("action")
            check(f"JSON修复: {raw[:24]}...", got == want, f"got={got!r}")
        except llm.LLMError as exc:
            check(f"JSON修复: {raw[:24]}...", False, str(exc))
    try:
        llm._extract_json("这不是 JSON 内容")
        check("JSON修复: 垃圾输入应报错", False)
    except llm.LLMError:
        check("JSON修复: 垃圾输入应报错", True)


# ------------------------------------------------------------------ LLM 配置指纹
def test_fingerprint() -> None:
    storage.init_db()
    f1 = llmcfg.fingerprint()
    storage.set_llm_config({"enabled": True, "base_url": "https://x/v1", "model": "m1", "api_key": "k", "json_mode": True})
    f2 = llmcfg.fingerprint()
    storage.set_llm_config({"enabled": True, "base_url": "https://x/v1", "model": "m2", "api_key": "k", "json_mode": True})
    f3 = llmcfg.fingerprint()
    check("指纹随配置变化", f1 != f2 and f2 != f3)
    storage.clear_llm_config()# ------------------------------------------------------------------ 模型列表过滤
# 云端 /models 里混有 embedding/图片/语音等非对话模型，应被过滤、对话模型应保留
def test_model_filter() -> None:
    keep = ["deepseek-chat", "deepseek-reasoner", "qwen-plus", "glm-4-plus",
            "moonshot-v1-32k", "gpt-4o", "llama3.1:70b", "kimi-k2-thinking"]
    drop = ["text-embedding-3-large", "bge-m3", "dall-e-3", "whisper-1",
            "sdxl-turbo", "rerank-3", "image-1", "flux-dev"]
    bad_keep = [m for m in keep if llm._NON_CHAT_RE.search(m)]
    bad_drop = [m for m in drop if not llm._NON_CHAT_RE.search(m)]
    check("模型过滤（保留对话/剔除非对话）", not bad_keep and not bad_drop,
          f"keep误杀={bad_keep} drop漏网={bad_drop}")


# ------------------------------------------------------------------ 资讯解读
def test_news_interpret() -> None:
    # 规则解读关键词情绪
    bull = news.rule_interpret({"title": "公司中标大订单", "summary": "业绩预增 50%"})
    bear = news.rule_interpret({"title": "股东减持", "summary": "收到处罚决定书"})
    flat = news.rule_interpret({"title": "召开股东大会", "summary": "审议日常议案"})
    check("资讯规则解读: 利好词", bull["sentiment"] == "利好" and bull["engine"] == "rule", str(bull))
    check("资讯规则解读: 利空词", bear["sentiment"] == "利空", str(bear))
    check("资讯规则解读: 中性", flat["sentiment"] == "中性", str(flat))

    # 资讯评分进规则引擎：利好加分、封顶 ±12、无资讯不引用
    detail = {
        "quote": {"code": "600000", "name": "浦发银行", "price": 9.0, "prev_close": 9.1, "change_pct": -1.1},
        "boards": [], "kline": [], "ma": [],
        "ma_summary": {"arrangement": "交织", "above_count": 0, "above": [], "below": [], "series": {}},
        "support_resistance": {}, "fund_flow": {"rows": [], "summary": {}},
        "margin": {"rows": [], "summary": {}},
        "status": {"tags": [], "trend": {}},
    }
    news_items = [
        {"title": "中标大单", "summary": "s", "date": "2026-08-17 10:00:00",
         "interpretation": {"sentiment": "利好", "impact": "高", "summary": "正面"}},
        {"title": "获准收购", "summary": "s", "date": "2026-08-16 10:00:00",
         "interpretation": {"sentiment": "利好", "impact": "中", "summary": "正面"}},
        {"title": "被罚款", "summary": "s", "date": "2026-08-15 10:00:00",
         "interpretation": {"sentiment": "利空", "impact": "中", "summary": "负面"}},
    ]
    fb = analysis.rule_based(detail, news_items)
    check("资讯评分写入建议依据", "资讯面 2 利好/1 利空（计 +4 分）" in fb["advice"]["reason"], fb["advice"]["reason"])
    check("资讯情绪进机会面", any("资讯面偏暖" in o for o in fb["risk"]["opportunities"]))
    fb0 = analysis.rule_based(detail, None)
    check("无资讯不引用", "资讯面" not in fb0["advice"]["reason"])
    payload = analysis.build_payload(detail, news_items)
    check("投喂数据含资讯段", len(payload.get("市场资讯_近30日") or []) == 3)


# ------------------------------------------------------------------ K 线滞后判定
def test_kline_stale() -> None:
    from datetime import datetime
    from backend.utils import kline_is_stale

    # 固定注入时间，避免 CI 在任意时刻运行时判据漂移
    mon_close = datetime(2026, 8, 17, 16, 0)   # 周一收盘后
    # 周一收盘：K线停在周五应判滞后，含今天则正常
    check("K线滞后判定: 周一收盘缺周五->周一(停08-14)", kline_is_stale("2026-08-14", mon_close) is True)
    check("K线滞后判定: 周一收盘已含今天(08-17)", kline_is_stale("2026-08-17", mon_close) is False)
    check("K线滞后判定: 盘中不判定", kline_is_stale("2026-08-14", datetime(2026, 8, 17, 11, 0)) is False)
    check("K线滞后判定: 空日期不滞后", kline_is_stale("", mon_close) is False)
    check("K线滞后判定: 非法日期不滞后", kline_is_stale("abc", mon_close) is False)


# ------------------------------------------------------------------ 缓存

def test_cache() -> None:
    async def run() -> None:
        c = cache.TTLCache()
        calls = {"n": 0}

        async def loader() -> str:
            calls["n"] += 1
            await asyncio.sleep(0.01)
            return "v"

        await asyncio.gather(*[c.get_or_set("k", 60, loader) for _ in range(5)])
        check("缓存单飞（并发只加载一次）", calls["n"] == 1, f"n={calls['n']}")
        check("缓存锁表回收", not c._locks)
        # 过期后应重新加载
        c._data["k"] = (0.0, "stale")
        await c.get_or_set("k", 60, loader)
        check("缓存过期重载", calls["n"] == 2, f"n={calls['n']}")
        # 上限淘汰后不超限
        for i in range(cache.MAX_ENTRIES + 50):
            c.put(f"bulk{i}", i, 100.0)
        check("缓存条目硬上限", len(c._data) <= cache.MAX_ENTRIES, f"n={len(c._data)}")

    asyncio.run(run())


# ------------------------------------------------------------------ AI 每股票单飞锁
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
        check("AI 每股票单飞", calls["n"] == 1 and all(x == "done" for x in r), f"n={calls['n']}")
        check("AI 锁表回收", "600000" not in api._ai_locks)

    asyncio.run(run())


# ------------------------------------------------------------------ 指标
def test_indicators() -> None:
    bars = [
        Bar(date=f"2026-01-{i:02d}", open=10 + i, close=10 + i * 0.5, high=12 + i, low=9 + i, volume=100.0)
        for i in range(1, 70)
    ]
    infos, summary = build_ma(bars, 20.0)
    check("均线 4 条", len(infos) == 4, str(len(infos)))
    check("均线汇总", "arrangement" in summary and "series" in summary)
    ma_values = {i.window: i.value for i in infos}
    sr = support_resistance(bars, 20.0, ma_values)
    check("支撑压力", bool(sr.get("support") and sr.get("resistance")), str(sr.get("state")))


# ------------------------------------------------------------------ 数据源装配（不触网）
def test_registry() -> None:
    reg = registry()
    names = [p.name for p in reg.providers]
    check("数据源装配", "eastmoney" in names and len(names) >= 3, str(names))
    check("健康度接口（不触网）", len(reg.health()) == len(names))


def main() -> int:
    test_json_repair()
    test_fingerprint()
    test_model_filter()
    test_news_interpret()
    test_kline_stale()
    test_cache()
    test_ai_lock()
    test_indicators()
    test_registry()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 共 {TOTAL} 项检查: {FAILED}")
        return 1
    print(f"全部通过（{TOTAL} 项检查）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
