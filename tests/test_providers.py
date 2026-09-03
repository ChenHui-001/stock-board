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
    import backend.providers.quote_race as pmod  # QUOTE_RACE_STAGGER 定义处（拆分后归属 quote_race）

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


# ============================================================
# P0-1 / P0-2 / P0-3 新增字段单测（mock 数据，零网络）
# ============================================================

def test_quote_vwap_and_deviation_pct() -> None:
    """P0-1：VWAP = amount / volume；deviation_pct = (price / vwap - 1) * 100。
    实测基线（PRD 2026-09-01 平安银行 14:46）：VWAP 11.8621 vs 1 分钟线累加
    真值 11.8618，偏差 +0.003%。本测试以同样的算法取一组代表性数据验证。
    """
    from backend.providers.base import Quote
    from backend.service import _fill_intraday_fields

    # 平安银行 2026-09-01 14:46 实测口径：amount≈1.18e9、volume≈1.0e8 → vwap≈11.86
    q = Quote(code="000001", market="SZ",
              price=11.92,
              volume=100_000_000.0,
              amount=1_186_210_000.0)
    _fill_intraday_fields(q)
    assert q.vwap is not None and abs(q.vwap - 11.8621) < 0.01, q.vwap
    assert q.deviation_pct is not None and abs(q.deviation_pct - 0.49) < 0.05, q.deviation_pct

    # volume=0：vwap 留空，不除零
    q2 = Quote(code="000001", market="SZ", price=11.92, volume=0.0, amount=0.0)
    _fill_intraday_fields(q2)
    assert q2.vwap is None and q2.deviation_pct is None

    # amount/volume 缺一：vwap 留空
    q3 = Quote(code="000001", market="SZ", price=11.92, volume=100.0, amount=None)
    _fill_intraday_fields(q3)
    assert q3.vwap is None and q3.deviation_pct is None

    # 现价远低于 VWAP：负偏离
    q4 = Quote(code="000001", market="SZ", price=10.0, volume=100.0, amount=1200.0)
    _fill_intraday_fields(q4)
    assert q4.vwap == 12.0
    assert q4.deviation_pct is not None and abs(q4.deviation_pct - (-16.667)) < 0.01


def test_quote_main_net_fields() -> None:
    """P0-2：Quote 持有 main_net_inflow / main_net_pct，构造时即可填。"""
    from backend.providers.base import Quote
    q = Quote(code="000001", market="SZ",
              price=11.92,
              main_net_inflow=60_196_496.0,
              main_net_pct=3.51)
    d = q.to_dict()
    assert d["main_net_inflow"] == 60_196_496.0
    assert d["main_net_pct"] == 3.51

    # 缺字段：保持 None，不报错
    q2 = Quote(code="000001", market="SZ")
    d2 = q2.to_dict()
    assert d2["main_net_inflow"] is None
    assert d2["main_net_pct"] is None


def test_board_dataclass_serialization() -> None:
    """P0-3：Board dataclass 与 list[str] 投影可双向互转。"""
    from backend.providers.base import Board
    from backend.service import _board_names

    rows = [
        Board(code="BK0475", market="90", name="银行Ⅱ", change_pct=1.97),
        Board(code="BK1610", market="90", name="股份制银行Ⅲ", change_pct=1.75),
        Board(code="BK0153", market="90", name="广东板块", change_pct=-0.08),
    ]
    # to_dict 形状
    d0 = rows[0].to_dict()
    assert d0 == {"code": "BK0475", "market": "90", "name": "银行Ⅱ", "change_pct": 1.97}, d0

    # 名字投影：保持与老 API list[str] 兼容
    names = _board_names(rows)
    assert names == ["银行Ⅱ", "股份制银行Ⅲ", "广东板块"], names

    # 缺 name 的 Board 在投影里被过滤（与原 list[str] 行为一致）
    rows2 = rows + [Board(code="BK0000", market="90", name="", change_pct=None)]
    names2 = _board_names(rows2)
    assert names2 == ["银行Ⅱ", "股份制银行Ⅲ", "广东板块"], names2


def test_board_roundtrip_through_cache_pack() -> None:
    """P0-3：_boards 缓存来回不丢字段。模拟 cache_pack 解码路径。"""
    from backend.providers.base import Board
    from backend.service import _board_names

    original = [
        Board(code="BK0475", market="90", name="银行Ⅱ", change_pct=1.97),
        Board(code="BK1610", market="90", name="股份制银行Ⅲ", change_pct=1.75),
    ]
    # 序列化（进入缓存）
    packed = {"rows": [b.to_dict() for b in original]}
    # 反序列化（从缓存恢复）
    valid_keys = set(Board.__annotations__.keys())
    decoded = [Board(**{k: v for k, v in d.items() if k in valid_keys}) for d in packed["rows"]]
    assert _board_names(decoded) == _board_names(original)
    assert decoded[0].change_pct == 1.97
    assert decoded[1].code == "BK1610"
