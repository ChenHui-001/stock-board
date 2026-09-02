"""P0-1 回归测试：K 线源单点失败必须只丢 K 线，不能拖垮整个详情页。

守护两处代码：
  1. backend/service.py `_kline()` 的 cached_pack(..., empty={"bars": [], "source": ""})
  2. backend/service.py `stock_detail()` 里 sources["errors"] 的筛选条件
     （pack.get("error") and not pack.get("stale")）

cached_pack（backend/service.py:41-62）取数失败时有三条分支，本模块全部覆盖：
  A. 无 stale 历史 + empty is None -> 原样 raise
     P0-1 修复前 _kline 走的就是这条：日线源一挂，stock_detail 的
     asyncio.wait(FIRST_EXCEPTION) 会把整个详情页打成 503，连行情/资金/两融
     这些本来成功的数据一起丢掉。
  B. 无 stale 历史 + 有 empty      -> 返回 {**empty, "stale": False, "error": ...}
     修复后 _kline 走这条：降级粒度与 kline_min / flow / margin 一致。
  C. 有 stale 历史                 -> 返回 {**stale, "stale": True, "error": ...}
     与 empty 无关。此时有旧数据可展示，只进 stale 列表、不进 errors。

不走网络：用 mock provider 替换 registry.providers。

注意：每个异步用例都显式带 @pytest.mark.asyncio。tests/conftest.py 的
自动打标钩子用 `co_flags & 0x100` 判断协程，而 CO_COROUTINE 实际是 0x80
（0x100 是 CO_ITERABLE_COROUTINE），导致 async def test_xxx 永远拿不到
asyncio 标记、被 pytest 静默 skip（不报错、不失败）。不要依赖那个钩子。
"""
from __future__ import annotations

from typing import Any

import pytest

from backend import service
from backend.cache import cache as _cache
from backend.providers import ProviderError, registry
from backend.providers.base import (
    Bar,
    Board,
    FinancialPeriod,
    FlowDay,
    MarginDay,
    Quote,
)

CODE = "000001"
MARKET = "SZ"
KLINE_KEY = f"kline:{CODE}.{MARKET}"
STALE_KLINE_KEY = f"stale:{KLINE_KEY}"
STALE_FLOW_KEY = f"stale:flow:{CODE}.{MARKET}"

# 断言用的失败文案，describe_exc 会带进 pack["error"]
DOWN_MSG = "K 线源 503（测试桩）"


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例前后清空缓存：_kline 的 empty/stale 分支完全取决于
    stale:kline:* 有没有历史，残留会让分支断言测错对象。"""
    _cache.clear()
    yield
    _cache.clear()


def _bars(n: int = 60) -> list[Bar]:
    """够算 MA60 的 K 线，避免退化到「数据不足」分支掩盖真实断言。"""
    return [
        Bar(
            date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            open=11.0 + i * 0.01,
            close=11.05 + i * 0.01,
            high=11.2 + i * 0.01,
            low=10.9 + i * 0.01,
            volume=1e8,
            amount=1.2e9,
        )
        for i in range(n)
    ]


class _FakeProvider:
    """最小可用 mock provider。

    quote / boards / kline_min / fund_flow / margin / financials 全部正常，
    只有 kline 与 fund_flow 的失败由构造参数控制，用于精确制造
    「K 线挂掉但其余源正常」和「资金流向走 stale 回退」两种场景。
    """

    name = "fake-degrade"
    caps = {"quotes", "boards", "kline", "kline_min", "fund_flow", "margin",
            "financials", "industry"}

    def __init__(self, kline_error: str | None = None,
                 flow_error: str | None = None) -> None:
        self._kline_error = kline_error
        self._flow_error = flow_error
        self._quote = Quote(
            code=CODE, market=MARKET, name="平安银行",
            price=11.92, prev_close=11.72, change=0.20, change_pct=1.71,
            open=11.78, high=11.95, low=11.65,
            volume=1e8, amount=1.186e9, turnover=0.75, volume_ratio=1.57,
        )
        self._boards = [Board(code="BK0475", market="90", name="银行Ⅱ", change_pct=1.97)]
        # 非空的 flow/margin/financials：registry.financials 没有 empty_ok，
        # 返回空列表会被 _first 判成「空结果」而失败，污染 errors 列表的断言
        self._flow_rows = [FlowDay(date="2026-09-01", main=1e8, close=11.92, change_pct=1.71)]
        self._margin_rows = [MarginDay(date="2026-09-01", rzye=1e9, rzrqye=1.1e9)]
        self._financials_rows = [FinancialPeriod(date="2026-06-30", period="2026H1",
                                                 revenue=1e10, revenue_yoy=5.0)]

    async def quote(self, code: str, market: str) -> Quote:
        return self._quote

    async def quotes(self, keys: list[tuple[str, str]]) -> dict[str, Quote]:
        from backend.utils import full_code as _fc
        return {_fc(c, m): self._quote for c, m in keys}

    async def boards(self, code: str, market: str) -> list[Board]:
        return self._boards

    async def kline(self, code: str, market: str, days: int) -> list[Bar]:
        if self._kline_error:
            raise ProviderError(self._kline_error)
        return _bars()

    async def kline_min(self, code: str, market: str, limit: int, klt: int = 60) -> list[Bar]:
        return _bars(24)

    async def fund_flow(self, code: str, market: str, days: int) -> list[FlowDay]:
        if self._flow_error:
            raise ProviderError(self._flow_error)
        return self._flow_rows

    async def margin(self, code: str, market: str, days: int) -> list[MarginDay]:
        return self._margin_rows

    async def financials(self, code: str, market: str, years: int) -> list[FinancialPeriod]:
        return self._financials_rows


def _patch_provider(monkeypatch, kline_error: str | None = None,
                    flow_error: str | None = None) -> None:
    """把 mock provider 注入 registry.providers 首位。

    写法与 test_intraday_fields_e2e._patch_provider 一致：monkeypatch.setattr
    保存原值并在 teardown 还原；_blocked_until / _fail 必须清掉，否则前一个
    用例里 kline 抛异常触发的熔断会让本用例连「可用源」都派不出去。
    """
    from backend.providers import ProviderStats

    fake = _FakeProvider(kline_error=kline_error, flow_error=flow_error)
    reg = registry()
    monkeypatch.setattr(reg, "providers", [fake], raising=False)
    monkeypatch.setattr(reg, "_stats", {fake.name: ProviderStats(name=fake.name)},
                        raising=False)
    reg._blocked_until.clear()
    reg._fail.clear()


# ---------------------------------------------------------------- cached_pack 三分支


async def _boom() -> Any:
    raise ProviderError(DOWN_MSG)


@pytest.mark.asyncio
async def test_cached_pack_raises_when_no_empty_and_no_stale() -> None:
    """分支 A：无 stale 历史 + empty is None -> 原样 raise。

    这正是 P0-1 修复前 _kline 的写法，回归意义在于：若有人把 empty= 参数删掉，
    这条会失败并提醒「K 线又会把详情页打成 503」。
    """
    with pytest.raises(ProviderError) as ei:
        await service.cached_pack("pack-a", 60.0, _boom, True)
    assert DOWN_MSG in str(ei.value)


@pytest.mark.asyncio
async def test_cached_pack_returns_empty_pack_when_empty_given() -> None:
    """分支 B：无 stale 历史 + 有 empty -> 返回 empty 包而不抛。

    stale 必须是 False：下游按 stale 判断是否要给用户打「数据过期」标记，
    而这里是「连旧数据都没有」，两者语义不同。
    """
    pack = await service.cached_pack(
        "pack-b", 60.0, _boom, True, empty={"bars": [], "source": ""}
    )
    assert pack["bars"] == [], pack
    assert pack["source"] == "", pack
    assert pack["stale"] is False, pack
    assert DOWN_MSG in pack["error"], pack


@pytest.mark.asyncio
async def test_cached_pack_prefers_stale_over_empty() -> None:
    """分支 C：有 stale 历史 -> 回退上一次成功数据，与 empty 无关。

    即使同时给了 empty，也必须先走 stale：能展示带延迟标记的旧数据，
    就别给用户一片空白。
    """
    old = {"bars": _bars(3), "source": "old-source"}
    _cache.put("stale:pack-c", old, 3600.0)

    pack = await service.cached_pack(
        "pack-c", 60.0, _boom, True, empty={"bars": [], "source": ""}
    )
    assert pack["bars"] == old["bars"], pack
    assert pack["source"] == "old-source", pack
    assert pack["stale"] is True, pack
    assert DOWN_MSG in pack["error"], pack


# ---------------------------------------------------------------- _kline 本身


@pytest.mark.asyncio
async def test_kline_degrades_to_empty_instead_of_raising(monkeypatch) -> None:
    """P0-1 核心：K 线源挂掉 + 无历史 -> 返回空包，不抛异常。

    修复前这里会 raise ProviderError，stock_detail 的
    asyncio.wait(FIRST_EXCEPTION) 随即把整个详情页打成 503。
    """
    _patch_provider(monkeypatch, kline_error=DOWN_MSG)
    assert _cache.peek(STALE_KLINE_KEY) is None, "有 stale 残留会走错分支"

    pack = await service._kline(CODE, MARKET, True)

    assert pack["bars"] == [], pack
    assert pack["source"] == "", pack
    assert pack["stale"] is False, pack
    assert DOWN_MSG in pack["error"], pack


# ---------------------------------------------------------------- stock_detail 端到端


@pytest.mark.asyncio
async def test_stock_detail_survives_kline_failure(monkeypatch) -> None:
    """K 线源挂掉时，其余模块必须照常返回，且失败信息进 sources.errors。"""
    _patch_provider(monkeypatch, kline_error=DOWN_MSG)

    # 修复前这行会抛 ProviderError -> 详情页 503
    detail = await service.stock_detail(CODE, MARKET, force=True)

    assert detail["kline"] == [], detail["kline"]
    assert detail["quote"]["name"] == "平安银行", detail["quote"]
    assert detail["quote"]["price"] == 11.92, detail["quote"]
    # 资金流向/两融/财报走的是正常路径，不该被 K 线拖累
    assert detail["sources"]["fund_flow"] == "fake-degrade", detail["sources"]
    assert detail["fund_flow"]["rows"], detail["fund_flow"]
    assert detail["margin"]["rows"], detail["margin"]
    assert detail["financials"]["rows"], detail["financials"]

    src = detail["sources"]
    assert src["kline"] == "", src
    assert "K线" in src["errors"], src
    assert "K线" not in src["stale"], src


@pytest.mark.asyncio
async def test_sources_errors_excludes_stale_sources(monkeypatch) -> None:
    """errors 只收「彻底无数据」的源；走了 stale 回退的源不进 errors。

    两次取数制造混合场景：
      第 1 次全成功 -> 写满 stale:kline:* 与 stale:flow:*
      第 2 次前手动删掉 stale:kline:*，只留 stale:flow:*
      -> K 线走 empty 分支（进 errors），资金流向走 stale 分支（只进 stale）
    """
    _patch_provider(monkeypatch)
    await service.stock_detail(CODE, MARKET, force=True)
    assert _cache.peek(STALE_KLINE_KEY) is not None, "第 1 次取数未写入 stale:kline"
    assert _cache.peek(STALE_FLOW_KEY) is not None, "第 1 次取数未写入 stale:flow"

    # 只抽掉 K 线的历史，制造「K 线无数据、资金流向有旧数据」
    _cache.drop(STALE_KLINE_KEY)
    assert _cache.peek(STALE_KLINE_KEY) is None
    assert _cache.peek(STALE_FLOW_KEY) is not None

    _patch_provider(monkeypatch, kline_error=DOWN_MSG, flow_error="资金源 502（测试桩）")
    detail = await service.stock_detail(CODE, MARKET, force=True)

    src = detail["sources"]
    # K 线：无历史可回退 -> 空结果，必须上报
    assert "K线" in src["errors"], src
    assert "K线" not in src["stale"], src
    # 资金流向：有旧数据可展示 -> 只标 stale，不进 errors
    assert "资金流向" in src["stale"], src
    assert "资金流向" not in src["errors"], src
    # 旧数据确实还在（不是被 empty 覆盖成空）
    assert detail["fund_flow"]["rows"], detail["fund_flow"]
    assert detail["fund_flow"]["stale"] is True, detail["fund_flow"]
    assert "资金源 502" in detail["fund_flow"]["error"], detail["fund_flow"]
