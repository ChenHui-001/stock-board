"""行情多源竞速调度（从 providers/__init__.py 拆出）。

多源并发竞速 + 错峰派发：先派评分最高的源；RACE_STAGGER 秒内没拿齐就再派
下一个源加入竞速（快源先答、慢源随后跟上），直到拿齐或全部源派完。只在
「全部源都已返回且仍有缺失」时才报全部不可用——绝不因短超时提前放弃。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..config import settings
from ..utils import data_is_stale, full_code, normalize_code, resolve_market, session_state
from .base import Provider, ProviderError, Quote, Throttled

if TYPE_CHECKING:  # 仅类型标注用，避免运行时循环导入
    from . import Registry

# 与拆分前保持同一 logger 名，日志输出行为完全一致
log = logging.getLogger("providers.registry")

QUOTE_BATCH = 60
# 多源竞速的错峰派发窗口（秒）：窗口内没拿齐就再派一个源加入竞速。
# 不是总预算——总预算由 HTTP_TIMEOUT×(RETRY+1)+3 兜底，确保慢源也有机会返回。
QUOTE_RACE_STAGGER = 1.2


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


class QuoteRaceMixin:
    """行情竞速（依赖宿主的 _available/_stat/_mark_ok/_mark_fail 方法）。"""

    async def _fetch_quotes(
        self: "Registry",
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
        self: "Registry",
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

    async def quotes(
        self: "Registry", keys: list[tuple[str, str]]
    ) -> tuple[dict[str, Quote], list[str]]:
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
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s 异常，按空数据继续: %s", "quotes", exc)
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

    async def quote(self: "Registry", code: str, market: str | None = None) -> Quote:
        market = market or resolve_market(code)
        data, _ = await self.quotes([(normalize_code(code), market)])
        key = full_code(code, market)
        quote = data.get(key)
        if not quote:
            raise ProviderError(f"未获取到 {key} 的行情")
        return quote
