"""数据源注册表与故障转移调度。

- 按 `PROVIDER_ORDER` 依次尝试，任一源抛异常/返回空即切下一个。
- 行情支持"部分补齐"：主源缺失的股票交给下一个源补，而不是整批重来。
- 每个源记录健康状态，连续失败会被短暂熔断，避免每次请求都卡在超时上。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Coroutine

import asyncio
from collections import deque
from dataclasses import dataclass, field

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
    Board,
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
# 多源竞速的错峰派发窗口（秒）：窗口内没拿齐就再派一个源加入竞速。
# 不是总预算——总预算由 HTTP_TIMEOUT×(RETRY+1)+3 兜底，确保慢源也有机会返回。
QUOTE_RACE_STAGGER = 1.2


@dataclass
class ProviderStats:
    """单个数据源的运行时质量统计（滑动窗口）。"""

    name: str
    ok: int = 0
    fail: int = 0
    total_ms: int = 0          # 成功请求总耗时（毫秒）
    last_ok_at: float = 0.0    # 最后一次成功时间戳
    last_quote_time: str = ""  # 最近一次行情时间
    _latencies: deque[int] = field(default_factory=lambda: deque(maxlen=20))

    def record(self, ok: bool, latency_ms: int, quote_time: str = "") -> None:
        if ok:
            self.ok += 1
            self.total_ms += latency_ms
            self._latencies.append(latency_ms)
            self.last_ok_at = time.monotonic()
            if quote_time:
                self.last_quote_time = quote_time
        else:
            self.fail += 1

    @property
    def requests(self) -> int:
        return self.ok + self.fail

    @property
    def success_rate(self) -> float:
        total = self.requests
        return self.ok / total if total else 1.0

    @property
    def avg_latency_ms(self) -> int:
        if not self._latencies:
            return 0
        return int(sum(self._latencies) / len(self._latencies))

    @property
    def score(self) -> float:
        """综合评分：成功率 70% + 延迟 30%。范围 0~1，越高越好。"""
        if self.requests == 0:
            return 0.5  # 无样本时给中等分，不贸然优先
        # 延迟分：假设 0ms=1.0，2000ms=0.0
        latency_score = max(0.0, 1.0 - self.avg_latency_ms / 2000)
        return round(self.success_rate * 0.7 + latency_score * 0.3, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "fail": self.fail,
            "success_rate": round(self.success_rate, 2),
            "avg_latency_ms": self.avg_latency_ms,
            "score": self.score,
            "last_ok_at": self.last_ok_at,
            "last_quote_time": self.last_quote_time,
        }


def _quote_freshness(q: Quote) -> int:
    """行情新鲜度打分：有最新价 > 有昨收 > 无。用于多源结果合并时择优。"""
    score = 0
    if q.price and q.price > 0:
        score += 100
    if q.prev_close and q.prev_close > 0:
        score += 10
    if q.quote_time:
        score += 1
    return score


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

    def _stat(self, name: str) -> ProviderStats:
        return self._stats.setdefault(name, ProviderStats(name=name))

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
        # 行情按综合评分动态排序：快且稳的源优先，减少固定顺序下
        # 东财频控对首屏的阻塞。
        if cap == "quotes":
            ready.sort(key=lambda p: self._stat(p.name).score, reverse=True)
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
        result: list[dict[str, Any]] = []
        for p in self.providers:
            stat = self._stat(p.name)
            result.append({
                "name": p.name,
                "caps": sorted(p.caps),
                "fails": self._fail.get(p.name, 0),
                "cooling": max(0, round(self._blocked_until.get(p.name, 0) - now)),
                "ok": stat.ok,
                "fail": stat.fail,
                "success_rate": round(stat.success_rate, 2),
                "avg_latency_ms": stat.avg_latency_ms,
                "score": stat.score,
                "last_quote_time": stat.last_quote_time,
            })
        return result

    @staticmethod
    def throttled_hosts() -> dict[str, float]:
        return limiter.status()

    def host_stats(self) -> dict[str, dict[str, Any]]:
        return limiter.host_stats()

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

    # ------------------------------------------------------------ 行情
    async def _fetch_quotes(
        self,
        provider: Provider,
        keys: list[tuple[str, str]],
    ) -> tuple[str, dict[str, Quote]]:
        """单个 provider 取一批行情，带统计打点。返回 (provider_name, {key: Quote})。"""
        start = time.monotonic()
        quote_time = ""
        try:
            part = await provider.quotes(keys)
        except Throttled as exc:
            log.debug("数据源 %s 行情频控：%s", provider.name, exc)
            self._stat(provider.name).record(False, int((time.monotonic() - start) * 1000))
            return provider.name, {}
        except Exception as exc:  # noqa: BLE001
            log.info("数据源 %s 行情失败：%s", provider.name, exc)
            self._mark_fail(provider.name)
            self._stat(provider.name).record(False, int((time.monotonic() - start) * 1000))
            return provider.name, {}
        latency_ms = int((time.monotonic() - start) * 1000)
        out: dict[str, Quote] = {}
        for key, quote in part.items():
            if not quote:
                continue
            quote.source = provider.name
            quote.latency_ms = latency_ms
            out[key] = quote
        if out:
            self._mark_ok(provider.name)
            best = max(out.values(), key=_quote_freshness, default=None)
            if best:
                quote_time = best.quote_time or best.trade_date
        self._stat(provider.name).record(True, latency_ms, quote_time)
        return provider.name, out

    def _merge_quotes(
        self,
        part: dict[str, Quote],
        result: dict[str, Quote],
        stash: dict[str, Quote],
        wanted: dict[str, Any],
        used: list[str],
        provider_name: str,
    ) -> None:
        """把单个 provider 返回的行情合并到结果集中。"""
        got_any = False
        for key, quote in part.items():
            if key not in wanted or key in result:
                continue
            if _usable(quote):
                result[key] = _fill_preopen(quote)
                got_any = True
            else:
                stash.setdefault(key, quote)
        if got_any and provider_name not in used:
            used.append(provider_name)

    async def quotes(self, keys: list[tuple[str, str]]) -> tuple[dict[str, Quote], list[str]]:
        """返回 (行情字典, 实际生效的数据源列表)。

        多源并发竞速 + 错峰派发：先派评分最高的源；RACE_STAGGER 秒内没拿齐
        就再派下一个源加入竞速（快源先答、慢源随后跟上），直到拿齐或全部源
        派完。只在「全部源都已返回且仍有缺失」时才报全部不可用——绝不因
        短超时提前放弃（旧实现 3s 总预算会在单源慢时误杀所有源，回归为
        「所有行情数据源均不可用」误报）。
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

        providers = self._available("quotes")
        if not providers:
            raise ProviderError("没有可用的行情数据源")

        missing = list(wanted.keys())
        provider_iter = iter(providers)
        pending: set[asyncio.Task] = set()
        exhausted = False   # 全部源是否已派发完

        def dispatch_next() -> None:
            nonlocal exhausted
            if not missing:
                return
            try:
                provider = next(provider_iter)
            except StopIteration:
                exhausted = True
                return
            # 当前缺失的股票交给该 provider 批量取
            need = [wanted[k] for k in missing]
            task = asyncio.create_task(self._fetch_quotes(provider, need))
            pending.add(task)

        # 先派第一个源
        dispatch_next()

        # 绝对兜底预算：覆盖单源最坏耗时（HTTP_TIMEOUT × 重试次数 + 退避），
        # 防止所有源都挂死时请求无限等待；正常情况下远在预算内就已拿齐。
        deadline = time.monotonic() + settings.HTTP_TIMEOUT * (settings.HTTP_RETRY + 1) + 3.0

        while missing:
            if not pending:
                if exhausted:
                    break  # 全部源已返回且仍有缺失 → 放弃
                dispatch_next()
                continue
            now = time.monotonic()
            if now >= deadline:
                break  # 绝对预算兜底
            # 错峰窗口：窗口内等任一源返回；到点仍未拿齐就再派一个源加入竞速
            wait = min(QUOTE_RACE_STAGGER, deadline - now)
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=wait
            )
            for task in done:
                try:
                    provider_name, part = await task
                except Exception:  # noqa: BLE001
                    continue
                self._merge_quotes(part, result, stash, wanted, used, provider_name)
                missing = [k for k in wanted if k not in result]
            # 窗口结束仍有缺失 → 派下一个源（没有可派的则标记耗尽）
            if missing:
                dispatch_next()

        # 取消剩余在途任务（已拿齐 / 预算到点 / 源耗尽）
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)

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
        except ProviderError:
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
