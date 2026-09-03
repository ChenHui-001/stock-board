"""数据源注册表与故障转移调度。

- 按 `PROVIDER_ORDER` 依次尝试，任一源抛异常/返回空即切下一个。
- 行情支持"部分补齐"：主源缺失的股票交给下一个源补，而不是整批重来。
- 每个源记录健康状态，连续失败会被短暂熔断，避免每次请求都卡在超时上。

模块拆分：
  - health.py      熔断与健康统计（ProviderStats / HealthMixin）
  - quote_race.py  行情多源竞速（QuoteRaceMixin 及行情合并/占位工具）
  - 本模块         Registry 组装、其余能力的通用调度与 re-export 契约
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Coroutine

from ..config import settings
from ..utils import chunked, describe_exc, normalize_code, resolve_market
from .base import (
    Bar,
    Board,
    FinancialPeriod,
    FlowDay,
    MarginDay,
    NewsItem,
    NotSupported,
    Provider,
    ProviderError,
    Quote,
    ReportItem,
    SearchItem,
    Throttled,
    close_client,
)
from .eastmoney import EastmoneyProvider
from .health import HealthMixin, ProviderStats
from .quote_race import QUOTE_BATCH, QuoteRaceMixin, _quote_freshness
from .sina import SinaProvider
from .tencent import TencentProvider
from .ths import ThsProvider

log = logging.getLogger("providers.registry")

_FACTORIES: dict[str, Callable[[], Provider]] = {
    "eastmoney": EastmoneyProvider,
    "tencent": TencentProvider,
    "sina": SinaProvider,
    "ths": ThsProvider,
}

if settings.ENABLE_AKSHARE:
    try:
        from .aks import AkshareProvider

        _FACTORIES["akshare"] = AkshareProvider
    except Exception as exc:  # noqa: BLE001
        log.warning("AkShare 数据源加载失败，已跳过：%s", exc)


class Registry(QuoteRaceMixin, HealthMixin):
    """数据源注册表：能力调度 + 行情竞速 + 熔断健康度。

    _fail / _blocked_until / _stats 三个实例属性由 HealthMixin 使用，
    必须在 __init__ 中先行创建（测试也会直接 monkeypatch 它们）。
    """

    def __init__(self) -> None:
        self.providers: list[Provider] = []
        self._fail: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}
        self._stats: dict[str, ProviderStats] = {}
        for name in settings.PROVIDER_ORDER:
            factory = _FACTORIES.get(name)
            if not factory:
                continue
            try:
                self.providers.append(factory())
                self._stats[name] = ProviderStats(name=name)
            except Exception as exc:  # noqa: BLE001
                log.warning("数据源 %s 初始化失败：%s", name, exc)
        if not self.providers:
            raise RuntimeError("没有任何可用数据源，请检查 PROVIDER_ORDER 配置")

    # ------------------------------------------------------------ 通用调度
    async def _first(
        self,
        cap: str,
        call: Callable[[Provider], Coroutine[Any, Any, Any]],
        *,
        empty_ok: bool = False,
    ) -> tuple[Any, str]:
        errors: list[str] = []
        for provider in self._available(cap):
            start = time.monotonic()
            quote_time = ""
            try:
                result = await call(provider)
            except NotSupported:
                continue
            except Throttled as exc:
                # 主机级频控已由 limiter 快速失败，不计入数据源健康度
                errors.append(f"{provider.name}: {describe_exc(exc)}")
                self._stat(provider.name).record(False, int((time.monotonic() - start) * 1000))
                continue
            except Exception as exc:  # noqa: BLE001
                self._mark_fail(provider.name)
                errors.append(f"{provider.name}: {describe_exc(exc)}")
                log.info("数据源 %s 的 %s 失败：%s", provider.name, cap, describe_exc(exc))
                self._stat(provider.name).record(False, int((time.monotonic() - start) * 1000))
                continue
            latency_ms = int((time.monotonic() - start) * 1000)
            if not result and not empty_ok:
                # 空结果是「这个源没有这条数据」，不是故障，不计入健康度
                errors.append(f"{provider.name}: 空结果")
                continue
            self._mark_ok(provider.name)
            # 对行情结果提取最新报价时间，用于新鲜度评分
            if cap == "quotes" and isinstance(result, dict):
                best = max(result.values(), key=_quote_freshness, default=None)
                if best:
                    quote_time = best.quote_time or best.trade_date
            self._stat(provider.name).record(True, latency_ms, quote_time)
            return result, provider.name
        raise ProviderError(f"{cap} 全部数据源失败 -> " + "; ".join(errors or ["无可用源"]))

    # ------------------------------------------------------------ 其他能力
    async def search(self, keyword: str, limit: int = 15) -> list[SearchItem]:
        items, _ = await self._first("search", lambda p: p.search(keyword, limit))
        return items

    async def search_with_source(self, keyword: str, limit: int = 15) -> tuple[list[SearchItem], str]:
        """检索股票并返回实际生效的数据源名（供热点关联股标注检索来源）。"""
        return await self._first("search", lambda p: p.search(keyword, limit))

    async def kline(self, code: str, market: str, limit: int) -> tuple[list[Bar], str]:
        bars, src = await self._first("kline", lambda p: p.kline(code, market, limit))
        return bars, src

    async def kline_min(self, code: str, market: str, limit: int,
                        klt: int = 60) -> tuple[list[Bar], str]:
        """分钟 K 线：仅支持的数据源返回。无可用源时返回 ([], "") 让上层降级。

        捕获 ProviderError 而不是 NotSupported，因为 _first 内部已经把所有 NotSupported
        收敛为最后一次 raise ProviderError；empty_ok=True 让数据源返回空 bars 时也算成功，
        与 fund_flow 的设计对齐。
        """
        try:
            bars, src = await self._first(
                "kline_min", lambda p: p.kline_min(code, market, limit, klt),
                empty_ok=True,
            )
            return bars or [], src or ""
        except ProviderError as exc:
            log.warning("%s 异常，按空数据继续: %s", "kline_min", exc)
            return [], ""

    async def fund_flow(self, code: str, market: str, days: int) -> tuple[list[FlowDay], str]:
        rows, src = await self._first(
            "fund_flow", lambda p: p.fund_flow(code, market, days), empty_ok=True
        )
        return rows or [], src

    async def margin(self, code: str, market: str, days: int) -> tuple[list[MarginDay], str]:
        rows, src = await self._first(
            "margin", lambda p: p.margin(code, market, days), empty_ok=True
        )
        return rows or [], src

    async def boards(self, code: str, market: str) -> list[Board]:
        """个股所属板块（带结构：code/market/name/change_pct）。
        失败/源不支持 → []，与原 list[str] 行为一致（上层兜底）。
        """
        try:
            rows, _ = await self._first("boards", lambda p: p.boards(code, market), empty_ok=True)
            return rows or []
        except ProviderError as exc:
            log.warning("%s 异常，按空数据继续: %s", "boards", exc)
            return []

    async def industry(self, keys: list[tuple[str, str]]) -> dict[str, str]:
        """批量行业查询：一次请求覆盖多只股票；失败/空结果返回 {}，由上层兜底。"""
        wanted = {
            f"{normalize_code(c)}.{m or resolve_market(c)}": (normalize_code(c), m or resolve_market(c))
            for c, m in keys
        }
        for provider in self._available("industry"):
            result: dict[str, str] = {}
            got_any = False
            for batch in chunked(list(wanted.values()), QUOTE_BATCH):
                try:
                    part = await provider.industry(batch)
                except NotSupported:
                    break
                except Throttled as exc:
                    log.debug("数据源 %s 行业查询频控：%s", provider.name, exc)
                    break
                except Exception as exc:  # noqa: BLE001
                    self._mark_fail(provider.name)
                    log.info("数据源 %s 行业查询失败：%s", provider.name, exc)
                    break
                if not part:
                    log.info("数据源 %s 行业查询返回空", provider.name)
                    break
                got_any = True
                result.update({k: v for k, v in part.items() if k in wanted and v})
            if got_any:
                self._mark_ok(provider.name)
                return result
        return {}

    async def hot(self, limit: int = 10) -> dict[str, list[Quote]]:
        data, _ = await self._first("hot", lambda p: p.hot(limit))
        return data

    async def news(
        self, code: str, market: str, name: str, days: int = 30, limit: int = 15
    ) -> list[NewsItem]:
        items, _ = await self.news_src(code, market, name, days, limit)
        return items

    async def news_src(
        self, code: str, market: str, name: str, days: int = 30, limit: int = 15
    ) -> tuple[list[NewsItem], str]:
        """个股资讯 + 实际生效数据源（东财不可用时自动回退新浪网页新闻）。"""
        items, src = await self._first(
            "news", lambda p: p.news(code, market, name, days, limit)
        )
        return items, src

    async def reports(
        self, code: str, market: str, limit: int = 15
    ) -> tuple[list[ReportItem], str]:
        """券商研报 + 实际生效数据源（同花顺主、东方财富辅）。"""
        items, src = await self._first(
            "reports", lambda p: p.reports(code, market, limit)
        )
        return items, src

    async def financials(
        self, code: str, market: str, limit: int = 12
    ) -> tuple[list[FinancialPeriod], str]:
        """定期报告核心指标 + 实际生效数据源（同花顺主、东方财富辅）。"""
        items, src = await self._first(
            "financials", lambda p: p.financials(code, market, limit)
        )
        return items, src


_registry: Registry | None = None


def registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry


async def shutdown() -> None:
    await close_client()


__all__ = [
    "Bar",
    "FinancialPeriod",
    "FlowDay",
    "MarginDay",
    "NewsItem",
    "Provider",
    "ProviderError",
    "Quote",
    "ReportItem",
    "SearchItem",
    "Throttled",
    "ProviderStats",
    "Registry",
    "registry",
    "shutdown",
]
