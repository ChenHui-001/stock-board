"""数据源注册表与故障转移调度。

- 按 `PROVIDER_ORDER` 依次尝试，任一源抛异常/返回空即切下一个。
- 行情支持"部分补齐"：主源缺失的股票交给下一个源补，而不是整批重来。
- 每个源记录健康状态，连续失败会被短暂熔断，避免每次请求都卡在超时上。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Coroutine

from ..config import settings
from ..utils import chunked, data_is_stale, describe_exc, full_code, normalize_code, resolve_market, session_state
from .base import (
    Bar,
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
    limiter,
)
from .eastmoney import EastmoneyProvider
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

# 熔断：连续失败 N 次后冷却 M 秒
BREAK_AFTER = 3
COOLDOWN = 60.0
QUOTE_BATCH = 60


def _usable(q: Quote) -> bool:
    """行情是否可用。

    盘前（9:15 前）各源普遍返回 "-" 或 0，此时有昨收即可展示，不必逐源穿透；
    盘中拿不到价格才是真异常（停牌或源故障），交给下一个源。
    """
    if q.price and q.price > 0:
        return True
    return bool(q.prev_close and q.prev_close > 0) and session_state() != "open"


def _fill_preopen(q: Quote) -> Quote:
    """盘前无成交价时以昨收占位，涨跌归零。"""
    if not (q.price and q.price > 0) and q.prev_close:
        q.price = q.prev_close
        q.change = 0.0
        q.change_pct = 0.0
    return q


class Registry:
    def __init__(self) -> None:
        self.providers: list[Provider] = []
        self._fail: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}
        for name in settings.PROVIDER_ORDER:
            factory = _FACTORIES.get(name)
            if not factory:
                continue
            try:
                self.providers.append(factory())
            except Exception as exc:  # noqa: BLE001
                log.warning("数据源 %s 初始化失败：%s", name, exc)
        if not self.providers:
            raise RuntimeError("没有任何可用数据源，请检查 PROVIDER_ORDER 配置")

    # ------------------------------------------------------------ 健康度
    def _available(self, cap: str) -> list[Provider]:
        now = time.monotonic()
        ready = [
            p for p in self.providers
            if cap in p.caps and self._blocked_until.get(p.name, 0) <= now
        ]
        if not ready:
            # 全部处于熔断中：放行以免整站不可用
            ready = [p for p in self.providers if cap in p.caps]
        # 资讯、研报和财报按用户指定的来源优先级独立调度：同花顺主，
        # 东方财富辅；不改变行情/资金等其他能力的 PROVIDER_ORDER。
        if cap in {"news", "reports", "financials"}:
            priority = {"ths": 0, "eastmoney": 1}
            ready.sort(key=lambda p: priority.get(p.name, 2))
        return ready

    def _mark_ok(self, name: str) -> None:
        self._fail.pop(name, None)
        self._blocked_until.pop(name, None)

    def _mark_fail(self, name: str) -> None:
        count = self._fail.get(name, 0) + 1
        self._fail[name] = count
        if count >= BREAK_AFTER:
            self._blocked_until[name] = time.monotonic() + COOLDOWN
            log.warning("数据源 %s 连续失败 %d 次，冷却 %.0fs", name, count, COOLDOWN)

    def health(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [
            {
                "name": p.name,
                "caps": sorted(p.caps),
                "fails": self._fail.get(p.name, 0),
                "cooling": max(0, round(self._blocked_until.get(p.name, 0) - now)),
            }
            for p in self.providers
        ]

    @staticmethod
    def throttled_hosts() -> dict[str, float]:
        return limiter.status()

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
            try:
                result = await call(provider)
            except NotSupported:
                continue
            except Throttled as exc:
                # 主机级频控已由 limiter 快速失败，不计入数据源健康度
                errors.append(f"{provider.name}: {describe_exc(exc)}")
                continue
            except Exception as exc:  # noqa: BLE001
                self._mark_fail(provider.name)
                errors.append(f"{provider.name}: {describe_exc(exc)}")
                log.info("数据源 %s 的 %s 失败：%s", provider.name, cap, describe_exc(exc))
                continue
            if not result and not empty_ok:
                # 空结果是「这个源没有这条数据」，不是故障，不计入健康度
                errors.append(f"{provider.name}: 空结果")
                continue
            self._mark_ok(provider.name)
            return result, provider.name
        raise ProviderError(f"{cap} 全部数据源失败 -> " + "; ".join(errors or ["无可用源"]))

    # ------------------------------------------------------------ 行情
    async def quotes(self, keys: list[tuple[str, str]]) -> tuple[dict[str, Quote], list[str]]:
        """返回 (行情字典, 实际生效的数据源列表)。

        单只股票粒度的故障转移：某个源没给出**有效**价格（盘前返回 "-"、
        新浪对停牌股返回 0）时，只把这几只交给下一个源补，而不是整批重来。
        全部源都拿不到有效价的股票，用已知的残缺信息拼成带状态标注的占位行情。
        """
        wanted = {
            f"{normalize_code(c)}.{m or resolve_market(c)}": (normalize_code(c), m or resolve_market(c))
            for c, m in keys
        }
        result: dict[str, Quote] = {}
        stash: dict[str, Quote] = {}   # 无有效价格但含名称/昨收等信息
        used: list[str] = []
        if not wanted:
            return result, used

        for provider in self._available("quotes"):
            missing = [v for k, v in wanted.items() if k not in result]
            if not missing:
                break
            got_any = False
            for batch in chunked(missing, QUOTE_BATCH):
                try:
                    part = await provider.quotes(batch)
                except NotSupported:
                    break
                except Throttled as exc:
                    log.debug("数据源 %s 行情频控：%s", provider.name, exc)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.info("数据源 %s 行情失败：%s", provider.name, exc)
                    self._mark_fail(provider.name)
                    break
                for key, quote in part.items():
                    if key not in wanted or key in result:
                        continue
                    if _usable(quote):
                        result[key] = _fill_preopen(quote)
                        got_any = True
                    else:
                        stash.setdefault(key, quote)
            if got_any:
                self._mark_ok(provider.name)
                used.append(provider.name)

        if not result and not stash:
            raise ProviderError("所有行情数据源均不可用")

        for key, (code, market) in wanted.items():
            if key not in result:
                result[key] = self._placeholder(key, code, market, stash.get(key))
        return result, used

    @staticmethod
    def _placeholder(key: str, code: str, market: str, partial: Quote | None) -> Quote:
        quote = partial or Quote(code=code, market=market)
        state = session_state()
        if quote.prev_close:
            # 盘前/周末本就没有当日成交，展示昨收即可，不算异常
            quote.price = quote.prev_close
            quote.change = 0.0
            quote.change_pct = 0.0
            if state == "open":
                quote.status, quote.status_text = "suspended", "股票停牌"
            else:
                quote.status, quote.status_text = "normal", ""
        else:
            quote.status = "unknown"
            quote.status_text = "数据更新延迟，暂无有效行情"
        if quote.status == "normal" and data_is_stale(quote.trade_date):
            quote.status, quote.status_text = "delayed", "数据更新延迟"
        return quote

    async def quote(self, code: str, market: str | None = None) -> Quote:
        market = market or resolve_market(code)
        data, _ = await self.quotes([(normalize_code(code), market)])
        key = full_code(code, market)
        quote = data.get(key)
        if not quote:
            raise ProviderError(f"未获取到 {key} 的行情")
        return quote

    # ------------------------------------------------------------ 其他能力
    async def search(self, keyword: str, limit: int = 15) -> list[SearchItem]:
        items, _ = await self._first("search", lambda p: p.search(keyword, limit))
        return items

    async def kline(self, code: str, market: str, limit: int) -> tuple[list[Bar], str]:
        bars, src = await self._first("kline", lambda p: p.kline(code, market, limit))
        return bars, src

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

    async def boards(self, code: str, market: str) -> list[str]:
        try:
            names, _ = await self._first("boards", lambda p: p.boards(code, market), empty_ok=True)
            return names or []
        except ProviderError:
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
    "Registry",
    "registry",
    "shutdown",
]
