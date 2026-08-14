"""通用工具：市场判定、交易时段、数值处理。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .config import settings

TZ = ZoneInfo(settings.TZ)

# 交易时段（沪深）
MORNING = (time(9, 30), time(11, 30))
AFTERNOON = (time(13, 0), time(15, 0))
# 集合竞价起点，用于判断"当日盘口已开始"
PRE_OPEN = time(9, 15)


def now() -> datetime:
    return datetime.now(TZ)


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def is_trading_now(ts: datetime | None = None) -> bool:
    """是否处于连续竞价时段（不含法定节假日判断，节假日行情不变化亦无副作用）。"""
    ts = ts or now()
    if not is_weekday(ts.date()):
        return False
    t = ts.time()
    return (MORNING[0] <= t <= MORNING[1]) or (AFTERNOON[0] <= t <= AFTERNOON[1])


def session_state(ts: datetime | None = None) -> str:
    """返回 pre_open / open / lunch_break / closed / weekend。"""
    ts = ts or now()
    if not is_weekday(ts.date()):
        return "weekend"
    t = ts.time()
    if t < PRE_OPEN:
        return "pre_open"
    if MORNING[0] <= t <= MORNING[1]:
        return "open"
    if MORNING[1] < t < AFTERNOON[0]:
        return "lunch_break"
    if AFTERNOON[0] <= t <= AFTERNOON[1]:
        return "open"
    return "closed"


# ---------------------------------------------------------------- 代码/市场

def resolve_market(code: str) -> str:
    """根据 6 位代码推断交易所：SH / SZ / BJ。"""
    code = normalize_code(code)
    if code.startswith(("60", "68", "90", "5", "11", "13", "204")):
        return "SH"
    if code.startswith(("43", "83", "87", "88", "920")):
        return "BJ"
    return "SZ"


def normalize_code(code: str) -> str:
    """去掉 sh/sz/bj 前缀、.SH 后缀，统一成 6 位数字代码。"""
    c = (code or "").strip().upper()
    for sep in (".", "_"):
        if sep in c:
            head, _, tail = c.partition(sep)
            c = head if head.isdigit() else tail
    if c[:2] in ("SH", "SZ", "BJ"):
        c = c[2:]
    return c.strip()


def secid(code: str, market: str | None = None) -> str:
    """东方财富 secid：1.沪市 / 0.深市与北交所。"""
    code = normalize_code(code)
    market = market or resolve_market(code)
    return f"{1 if market == 'SH' else 0}.{code}"


_TX_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


def tencent_code(code: str, market: str | None = None) -> str:
    code = normalize_code(code)
    market = market or resolve_market(code)
    return f"{_TX_PREFIX.get(market, 'sh')}{code}"


def sina_code(code: str, market: str | None = None) -> str:
    return tencent_code(code, market)


def full_code(code: str, market: str | None = None) -> str:
    code = normalize_code(code)
    return f"{code}.{market or resolve_market(code)}"


# ---------------------------------------------------------------- 数值

def to_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        f = float(value)
        return default if f != f else f  # NaN
    s = str(value).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "null", "None"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def round2(value: Any) -> float | None:
    f = to_float(value)
    return None if f is None else round(f, 2)


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def today_str() -> str:
    return now().strftime("%Y-%m-%d")


def data_is_stale(trade_date: str | None) -> bool:
    """行情日期是否落后于最近一个交易日（粗略：落后 >1 自然日且非周末顺延）。"""
    if not trade_date:
        return False
    try:
        d = datetime.strptime(trade_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    ref = now().date()
    # 回退到最近一个工作日
    while not is_weekday(ref):
        ref -= timedelta(days=1)
    # 开盘前，最新数据本就是上一交易日
    if session_state() in ("pre_open", "weekend") or (
        is_weekday(now().date()) and now().time() < MORNING[0]
    ):
        ref -= timedelta(days=1)
        while not is_weekday(ref):
            ref -= timedelta(days=1)
    return (ref - d).days > 3
