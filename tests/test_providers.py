"""Providers。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from backend import (
    analysis, api, cache, check_sources, hotspot, hotspot_ai, hotspot_search,
    indicators, llm, llmcfg, metrics, news, providers, reports, scorecfg,
    service, storage, value_screener, valuecfg,
)
from backend.config import settings
from backend.indicators import build_ma, summarize_flow, support_resistance
from backend.providers import registry
from backend.providers.base import Bar, FlowDay

def test_model_filter() -> None:
    keep = ["deepseek-chat", "deepseek-reasoner", "qwen-plus", "glm-4-plus",
            "moonshot-v1-32k", "gpt-4o", "llama3.1:70b", "kimi-k2-thinking"]
    drop = ["text-embedding-3-large", "bge-m3", "dall-e-3", "whisper-1",
            "sdxl-turbo", "rerank-3", "image-1", "flux-dev"]
    bad_keep = [m for m in keep if llm._NON_CHAT_RE.search(m)]
    bad_drop = [m for m in drop if not llm._NON_CHAT_RE.search(m)]
    assert (not bad_keep and not bad_drop), f"keep误杀={bad_keep} drop漏网={bad_drop}"





def test_quote_racing() -> None:
    from backend.providers import Registry
    from backend.providers import base as pbase
    import backend.providers as pmod

    class FakeProvider(pbase.Provider):
        def __init__(self, name, delay, codes=None, fail=False):
            super().__init__(name=name, caps={"quotes"})
            self.delay = delay
            self.codes = codes
            self.fail = fail

        async def quotes(self, keys):
            await asyncio.sleep(self.delay)
            if self.fail:
                raise pbase.ProviderError(f"{self.name} 挂了")
            out = {}
            for code, market in keys:
                if self.codes and code not in self.codes:
                    continue
                out[f"{code}.{market}"] = pbase.Quote(
                    code=code, market=market, name=self.name, price=10.0,
                    prev_close=9.5, source=self.name)
            return out

    reg = Registry.__new__(Registry)  # 跳过 __init__ 的真实源装配
    reg.providers = []
    reg._fail = {}
    reg._blocked_until = {}
    reg._stats = {}

    orig_stagger = pmod.QUOTE_RACE_STAGGER
    pmod.QUOTE_RACE_STAGGER = 0.2
    try:
        # 场景1：两个慢源（30s）+ 一个快源 → 错峰派发后快源胜出，绝不因超时放弃
        reg.providers = [
            FakeProvider("slow1", 30.0),
            FakeProvider("slow2", 30.0),
            FakeProvider("fast", 0.0),
        ]
        t0 = time.time()
        quotes, used = asyncio.run(reg.quotes([("600000", "SH")]))
        dt = time.time() - t0
        q = quotes.get("600000.SH")
        assert (q is not None and q.source == "fast"), f"used={used}"
        assert (dt < 5.0), f"dt={dt:.1f}s"

        # 场景2：全部源失败 → 才报「所有行情数据源均不可用」
        reg.providers = [FakeProvider("bad1", 0.0, fail=True),
                         FakeProvider("bad2", 0.0, fail=True)]
        try:
            asyncio.run(reg.quotes([("600000", "SH")]))
            assert (False), "未抛错"
        except Exception as exc:  # noqa: BLE001
            assert ("均不可用" in str(exc)), str(exc)

        # 场景3：部分补齐 —— A 只有 600000，B 补齐 000001
        reg.providers = [
            FakeProvider("half", 0.0, codes=["600000"]),
            FakeProvider("full", 0.05),
        ]
        quotes, used = asyncio.run(reg.quotes([("600000", "SH"), ("000001", "SZ")]))
        assert (quotes.get("600000.SH") is not None and quotes.get("000001.SZ") is not None), f"keys={sorted(quotes.keys())} used={used}"

        # 场景4：首个源频控快速失败 → 立即派下一个源接管（东财频控切腾讯的路径）
        reg.providers = [FakeProvider("throttled", 0.0, fail=True),
                         FakeProvider("backup", 0.0)]
        quotes, used = asyncio.run(reg.quotes([("600000", "SH")]))
        q = quotes.get("600000.SH")
        assert (q is not None and q.source == "backup"), f"used={used}"
    finally:
        pmod.QUOTE_RACE_STAGGER = orig_stagger





def test_registry() -> None:
    reg = registry()
    names = [p.name for p in reg.providers]
    assert ("eastmoney" in names and len(names) >= 3), str(names)
    assert (len(reg.health()) == len(names))
    # 资讯/研报/财报能力：同花顺主源，东方财富辅源，资讯保留新浪末级兜底
    news_caps = sorted(p.name for p in reg.providers if "news" in p.caps)
    report_caps = sorted(p.name for p in reg.providers if "reports" in p.caps)
    financial_caps = sorted(p.name for p in reg.providers if "financials" in p.caps)
    assert (news_caps == ["eastmoney", "sina", "ths"]), str(news_caps)
    assert (report_caps == ["eastmoney", "ths"]), str(report_caps)
    assert (financial_caps == ["eastmoney", "ths"]), str(financial_caps)
    ordered_news = [p.name for p in reg._available("news")]
    ordered_reports = [p.name for p in reg._available("reports")]
    assert (ordered_news[:2] == ["ths", "eastmoney"] and ordered_reports[:2] == ["ths", "eastmoney"]), str((ordered_news, ordered_reports))


