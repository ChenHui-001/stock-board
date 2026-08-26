"""数据源抽象层。

每个 Provider 按能力实现方法，未实现的能力抛 `NotSupported`，
上层 `registry` 按 PROVIDER_ORDER 顺序做故障转移。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("providers")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.HTTP_TIMEOUT),
            headers={"User-Agent": UA, "Accept": "*/*"},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def fetch(url: str, *, headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
    """带主机级节流的 GET。

    被频控的主机直接抛 `Throttled`，由上层立刻切换数据源；
    只有偶发的超时/网络抖动才重试。
    """
    host = httpx.URL(url).host
    last: Exception | None = None
    for attempt in range(settings.HTTP_RETRY + 1):
        await limiter.acquire(host)   # 冷却中会直接抛 Throttled
        try:
            resp = await client().get(url, headers=headers, **kwargs)
            resp.raise_for_status()
            limiter.relax(host)
            return resp
        except Exception as exc:  # noqa: BLE001 - 交由上层做源切换
            last = exc
            throttled = isinstance(exc, _THROTTLE_ERRORS) or (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in (403, 429, 456, 503)
            )
            if throttled:
                limiter.punish(host)
                raise Throttled(f"{host} 触发频控: {exc}") from exc
            if attempt < settings.HTTP_RETRY:
                await asyncio.sleep(0.3 * (2 ** attempt))
    raise last  # type: ignore[misc]


class NotSupported(Exception):
    """该数据源不提供此能力。"""


class ProviderError(Exception):
    """该数据源本次取数失败，可切换下一个源。"""


# ------------------------------------------------------------------ 主机限流
# 东方财富等公开接口对单 IP 有频控，突发请求会被直接断连
# （表现为 httpx.RemoteProtocolError / curl exit 56）。
#
# 关键：被频控后**不能靠等待硬扛**——那会把每个请求拖成几十秒。
# 正确做法是把该主机标记为冷却中并立即失败，让上层瞬间切换到其他数据源。

_HOST_MIN_INTERVAL: dict[str, float] = {
    "push2his.eastmoney.com": 0.35,
    "push2.eastmoney.com": 0.20,
    "datacenter-web.eastmoney.com": 0.35,
    "searchapi.eastmoney.com": 0.25,
}
DEFAULT_MIN_INTERVAL = 0.12
COOLDOWN_START = 5.0
COOLDOWN_MAX = 60.0


class Throttled(ProviderError):
    """目标主机处于频控冷却中，应立即切换数据源而不是等待。"""


class HostLimiter:
    """主机级节流。计时统一用 `time.monotonic()`：与 Registry 的熔断计时同源，
    且不依赖事件循环，`status()` 在任意上下文都能安全调用。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._cooldown_len: dict[str, float] = {}
        self._guard = asyncio.Lock()

    async def _lock(self, host: str) -> asyncio.Lock:
        async with self._guard:
            if host not in self._locks:
                self._locks[host] = asyncio.Lock()
            return self._locks[host]

    async def acquire(self, host: str) -> None:
        until = self._cooldown_until.get(host, 0.0)
        now = time.monotonic()
        if until > now:
            raise Throttled(f"{host} 频控冷却中，剩余 {until - now:.0f}s")

        interval = _HOST_MIN_INTERVAL.get(host, DEFAULT_MIN_INTERVAL)
        lock = await self._lock(host)
        async with lock:
            wait = self._last.get(host, 0.0) + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()

    def punish(self, host: str) -> None:
        length = min(COOLDOWN_MAX, (self._cooldown_len.get(host) or COOLDOWN_START / 2) * 2)
        self._cooldown_len[host] = length
        self._cooldown_until[host] = time.monotonic() + length
        log.warning("主机 %s 触发频控，冷却 %.0fs（期间自动切换其他数据源）", host, length)

    def relax(self, host: str) -> None:
        self._cooldown_until.pop(host, None)
        current = self._cooldown_len.get(host)
        if current and current > COOLDOWN_START:
            self._cooldown_len[host] = current / 2
        else:
            self._cooldown_len.pop(host, None)

    def status(self) -> dict[str, float]:
        now = time.monotonic()
        return {
            host: round(until - now, 1)
            for host, until in self._cooldown_until.items()
            if until > now
        }


limiter = HostLimiter()
_THROTTLE_ERRORS = (httpx.RemoteProtocolError, httpx.ConnectError)


# ------------------------------------------------------------------ 数据模型

@dataclass
class Quote:
    code: str
    market: str
    name: str = ""
    board: str = ""            # 所属板块（细分行业）
    price: float | None = None      # 现价
    prev_close: float | None = None  # 昨收
    change: float | None = None      # 涨跌额
    change_pct: float | None = None  # 涨跌幅 %
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None      # 成交量（股）
    amount: float | None = None      # 成交额（元）
    turnover: float | None = None    # 换手率 %
    volume_ratio: float | None = None  # 量比
    trade_date: str = ""
    status: str = "normal"           # normal / suspended / delayed / unknown
    status_text: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Bar:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float = 0.0
    amount: float = 0.0
    change_pct: float | None = None
    turnover: float | None = None
    # ATR(14)：在 service.py 由 indicators.compute_atr 注入；数据不足或
    # 计算失败时为 None，便于上层判定是否启用 ATR 突破/归一化逻辑
    atr: float | None = None


@dataclass
class FlowDay:
    date: str
    main: float = 0.0        # 主力净流入
    xl: float = 0.0          # 超大单
    lg: float = 0.0          # 大单
    md: float = 0.0          # 中单
    sm: float = 0.0          # 小单（散户）
    main_pct: float | None = None
    close: float | None = None
    change_pct: float | None = None


@dataclass
class MarginDay:
    date: str
    rzye: float | None = None    # 融资余额
    rzmre: float | None = None   # 融资买入额
    rzche: float | None = None   # 融资偿还额
    rzjme: float | None = None   # 融资净买入
    rqye: float | None = None    # 融券余额
    rqmcl: float | None = None   # 融券卖出量
    rqyl: float | None = None    # 融券余量
    rzrqye: float | None = None  # 两融余额
    rzyezb: float | None = None  # 融资余额占流通市值比 %


@dataclass
class SearchItem:
    code: str
    market: str
    name: str
    board: str = ""
    type: str = "A股"


@dataclass
class NewsItem:
    """个股资讯条目。"""

    id: str = ""            # 文章 ID
    date: str = ""          # 发布时间 YYYY-MM-DD HH:MM:SS
    source: str = ""        # 来源媒体
    title: str = ""
    summary: str = ""       # 内容摘要
    url: str = ""


@dataclass
class ReportItem:
    """券商研报条目（同花顺研报数据）。"""

    id: str = ""
    date: str = ""          # 研报日期 YYYY-MM-DD
    source: str = ""        # 券商机构
    researcher: str = ""    # 研究员
    rating: str = ""        # 评级（买入/增持/中性…）
    title: str = ""
    url: str = ""


@dataclass
class FinancialPeriod:
    """上市公司定期报告核心指标（季报/中报/三季报/年报）。"""

    date: str = ""                  # 报告期末 YYYY-MM-DD
    period: str = ""                # 2026Q1 / 2026H1 / 2026Q3 / 2025FY
    revenue: float | None = None
    revenue_yoy: float | None = None
    net_profit: float | None = None
    net_profit_yoy: float | None = None
    net_profit_deduct: float | None = None
    net_profit_deduct_yoy: float | None = None
    eps: float | None = None
    roe: float | None = None
    gross_margin: float | None = None
    debt_ratio: float | None = None
    source: str = ""


@dataclass
class Provider:
    name: str = "base"
    caps: set[str] = field(default_factory=set)

    async def quotes(self, keys: list[tuple[str, str]]) -> dict[str, Quote]:
        raise NotSupported

    async def search(self, keyword: str, limit: int = 15) -> list[SearchItem]:
        raise NotSupported

    async def kline(self, code: str, market: str, limit: int) -> list[Bar]:
        raise NotSupported

    async def kline_min(self, code: str, market: str, limit: int, klt: int = 60) -> list[Bar]:
        """分钟级 K 线（默认 60 分钟）。多数数据源不提供，调用方需捕获 NotSupported
        做降级。仅 eastmoney 等少数源支持。"""
        raise NotSupported

    async def fund_flow(self, code: str, market: str, days: int) -> list[FlowDay]:
        raise NotSupported

    async def margin(self, code: str, market: str, days: int) -> list[MarginDay]:
        raise NotSupported

    async def hot(self, limit: int = 10) -> dict[str, list[Quote]]:
        raise NotSupported

    async def boards(self, code: str, market: str) -> list[str]:
        raise NotSupported

    async def industry(self, keys: list[tuple[str, str]]) -> dict[str, str]:
        """批量返回所属行业（细分行业）：{code.market: 行业名}，一次请求覆盖多只。"""
        raise NotSupported

    async def news(
        self, code: str, market: str, name: str, days: int = 30, limit: int = 15
    ) -> list[NewsItem]:
        """个股资讯（近 days 天），按时间倒序。"""
        raise NotSupported

    async def reports(self, code: str, market: str, limit: int = 15) -> list[ReportItem]:
        """券商研报，按日期倒序。"""
        raise NotSupported

    async def financials(self, code: str, market: str, limit: int = 12) -> list[FinancialPeriod]:
        """上市公司定期报告核心指标，按报告期倒序。"""
        raise NotSupported
