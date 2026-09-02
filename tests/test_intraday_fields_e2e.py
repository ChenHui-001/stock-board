"""P0-1/2/3 端到端验证：用 mock provider 替换真实数据源，
跑 service.stock_detail() 的完整路径，断言新字段出现在响应里。

不走网络、不依赖外部接口，覆盖：
  - Quote.vwap / deviation_pct（_fill_intraday_fields）
  - Quote.main_net_inflow / main_net_pct（provider 字段映射）
  - 响应里 boards（names）与 boards_detail（带结构）的双发
  - /stock/{code} 与 /quote/{code} 两端点同时携带新字段
"""
from __future__ import annotations

import asyncio
from typing import Any

from backend.providers import registry
from backend.providers.base import (
    Bar,
    Board,
    Quote,
)
from backend import service


class _FakeProvider:
    """最小可用的 mock provider，覆盖 quote / boards / kline_min / kline /
    flow / margin / financials / industry / hot / search 全部 NotSupported 默认行为。"""

    name = "fake"
    caps = {"quotes", "boards", "kline", "kline_min", "fund_flow", "margin",
            "financials", "industry"}

    def __init__(self) -> None:
        self._quote = Quote(
            code="000001", market="SZ",
            name="平安银行",
            price=11.92,
            prev_close=11.72,
            change=0.20,
            change_pct=1.71,
            open=11.78, high=11.95, low=11.65,
            volume=100_000_000.0,
            amount=1_186_210_000.0,
            turnover=0.75,
            volume_ratio=1.57,
            # P0-2：主力资金
            main_net_inflow=60_196_496.0,
            main_net_pct=3.51,
            quote_time="14:46:03",
        )
        self._boards = [
            Board(code="BK0475", market="90", name="银行Ⅱ", change_pct=1.97),
            Board(code="BK1610", market="90", name="股份制银行Ⅲ", change_pct=1.75),
            Board(code="BK0153", market="90", name="广东板块", change_pct=-0.08),
            Board(code="BK0500", market="90", name="HS300_", change_pct=0.13),
        ]
        self._bars = [
            Bar(date="2026-08-29", open=11.5, close=11.6, high=11.65, low=11.45,
                volume=80_000_000.0, amount=920_000_000.0),
            Bar(date="2026-09-01", open=11.68, close=11.72, high=11.95, low=11.65,
                volume=100_000_000.0, amount=1_186_210_000.0),
        ]
        self._flow_rows = []
        self._margin_rows = []
        self._financials_rows = []

    async def quote(self, code: str, market: str) -> Quote:
        return self._quote

    async def quotes(self, keys: list[tuple[str, str]]) -> dict[str, Quote]:
        """mock 行情批量接口：registry._fetch_quotes 调用 provider.quotes(keys)，
        返回 {full_code: Quote} 字典。"""
        from backend.utils import full_code as _fc
        return {_fc(c, m): self._quote for c, m in keys}

    async def boards(self, code: str, market: str) -> list[Board]:
        return self._boards

    async def kline(self, code: str, market: str, days: int) -> list[Bar]:
        return self._bars

    async def kline_min(self, code: str, market: str, limit: int) -> list[Bar]:
        return []

    async def fund_flow(self, code: str, market: str, days: int) -> list:
        return self._flow_rows

    async def margin(self, code: str, market: str, days: int) -> list:
        return self._margin_rows

    async def financials(self, code: str, market: str, years: int) -> list:
        return self._financials_rows


def _patch_provider(monkeypatch) -> None:
    """把 _FakeProvider 注入到 registry.providers 首位。
    monkeypatch.setattr 会保存当前值并在 teardown 还原，所以正确的顺序是：
      1. 记录原始引用以便 finalizer 兜底
      2. monkeypatch.setattr(reg, 'providers', [fake])  ← 此时 monkeypatch
         保存了原始 providers 列表；teardown 时还原
      3. 同样处理 _stats / _blocked_until / _fail
    """
    from backend.providers import ProviderStats
    fake = _FakeProvider()
    reg = registry()

    # 1. monkeypatch 在「原值」上注册 undo，setattr 之后再覆盖
    monkeypatch.setattr(reg, "providers", [fake], raising=False)
    # _stats：原 dict 备份后整体替换为 {fake.name: ProviderStats(fake.name)}
    orig_stats = dict(reg._stats)
    monkeypatch.setattr(reg, "_stats", {fake.name: ProviderStats(name=fake.name)},
                        raising=False)

    # 2. 清熔断（不留 undo，因为本来就是空 dict 不影响后续测试）
    reg._blocked_until.clear()
    reg._fail.clear()


def test_stock_detail_carries_p01_p02_p03_fields(monkeypatch) -> None:
    """端到端：mock provider → service.stock_detail → 响应同时携带 vwap /
    deviation_pct / main_net_* / boards_detail。"""
    _patch_provider(monkeypatch)
    # 清缓存，避免之前的 run 残留
    service.cache.clear()

    detail = asyncio.run(service.stock_detail("000001", "SZ", force=True))

    # P0-1：VWAP / deviation_pct
    q = detail["quote"]
    assert q.get("vwap") is not None, "vwap missing"
    assert abs(q["vwap"] - 11.8621) < 0.01, q["vwap"]
    assert q.get("deviation_pct") is not None, "deviation_pct missing"
    assert abs(q["deviation_pct"] - 0.49) < 0.05, q["deviation_pct"]

    # P0-2：主力资金
    assert q.get("main_net_inflow") == 60_196_496.0, q.get("main_net_inflow")
    assert q.get("main_net_pct") == 3.51, q.get("main_net_pct")

    # P0-3：boards（names）向后兼容 + boards_detail（结构化）双发
    assert detail["boards"] == ["银行Ⅱ", "股份制银行Ⅲ", "广东板块", "HS300_"], detail["boards"]
    detail_boards = detail["boards_detail"]
    assert len(detail_boards) == 4, detail_boards
    assert detail_boards[0] == {"code": "BK0475", "market": "90",
                                 "name": "银行Ⅱ", "change_pct": 1.97}, detail_boards[0]
    # 包含 change_pct=null 的条目
    assert all("change_pct" in b for b in detail_boards)


def test_quote_endpoint_carries_p01_p02_fields(monkeypatch) -> None:
    """/quote/{code} 端点：5 秒 tick 也必须带回 vwap / main_net_*。"""
    _patch_provider(monkeypatch)
    service.cache.clear()

    q = asyncio.run(service.get_quote("000001", "SZ", force=True))
    d = q.to_dict()
    assert d.get("vwap") is not None, d
    assert d.get("deviation_pct") is not None, d
    assert d.get("main_net_inflow") == 60_196_496.0, d
    assert d.get("main_net_pct") == 3.51, d


def test_boards_endpoint_carries_structured(monkeypatch) -> None:
    """boards_detail 公开 API：返回 list[Board]（带 change_pct）。"""
    _patch_provider(monkeypatch)
    service.cache.clear()

    rows = asyncio.run(service.boards_detail("000001", "SZ"))
    assert len(rows) == 4, rows
    assert rows[0].code == "BK0475"
    assert rows[0].change_pct == 1.97
    # 老 API 仍然返回纯名字
    names = asyncio.run(service.boards("000001", "SZ"))
    assert names == ["银行Ⅱ", "股份制银行Ⅲ", "广东板块", "HS300_"], names


def test_vwap_skipped_when_volume_zero(monkeypatch) -> None:
    """早盘集合竞价等场景 amount/volume=0 时，vwap 必须为 None，不抛异常。"""
    _patch_provider(monkeypatch)
    fake_obj = registry().providers[0]
    fake_obj._quote.volume = 0.0
    fake_obj._quote.amount = 0.0
    service.cache.clear()

    q = asyncio.run(service.get_quote("000001", "SZ", force=True))
    d = q.to_dict()
    assert d["vwap"] is None, d
    assert d["deviation_pct"] is None, d
