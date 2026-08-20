"""通用工具：市场判定、交易时段、数值处理。"""
from __future__ import annotations

import hashlib
import json
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


def describe_exc(exc: BaseException) -> str:
    """异常的简短描述，保证非空。

    httpx 的超时类异常（ReadTimeout / ConnectTimeout 等）由 anyio 的无参
    TimeoutError 转换而来，str() 是空串。直接把异常插值进报错文案会得到
    「全部数据源失败 -> eastmoney: ; sina: 」这种只剩冒号的提示，
    排查时完全看不出是超时还是别的故障，因此空消息一律回退到异常类名。
    """
    detail = str(exc).strip()
    return detail or type(exc).__name__


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


def items_fingerprint(
    items: Iterable[dict[str, Any]],
    fields: tuple[str, ...] = ("id", "date", "title"),
) -> str:
    """按条目关键字段生成稳定指纹，用于解读缓存 key。

    资讯/研报的逐条解读必须与条目一一对应；若解读缓存只按 code+days 存，
    原始数据刷新后新条目会命中旧解读（错位）。把条目指纹并入缓存 key 后，
    条目一变 key 就变，旧解读自然失效、按新条目重新解读。
    """
    payload = [
        {field: str(item.get(field, "")) for field in fields}
        for item in items
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def confidence(n: int) -> dict[str, str]:
    """按样本量标注统计置信度：样本越深结论越可靠。

    返回 {"level": "high|medium|low", "label": "高|中|低", "note": 说明}。
    供自检面板（check_sources）与离线回测（backtest_intraday）共用，
    保证两处口径一致；分档与回测校准的「样本<50 仅参考」阈值衔接：
    ≥100 高 / 50-99 中 / <50 低。
    """
    if n >= 100:
        return {"level": "high", "label": "高", "note": f"样本 {n} 个，结论较可靠"}
    if n >= 50:
        return {"level": "medium", "label": "中", "note": f"样本 {n} 个，参考价值一般"}
    return {"level": "low", "label": "低", "note": f"样本 {n} 个，仅作参考"}


def today_str() -> str:
    return now().strftime("%Y-%m-%d")


def data_is_stale(trade_date: str | None) -> bool:
    """行情日期是否明显落后于最近一个交易日。

    阈值取 3 天（而非 1 天）：容忍周末顺延、调休与短假期造成的正常空档，
    只有真正长时间不更新才判为「延迟」，避免节后开盘前误报。
    盘前与周末参照日再回退一个交易日——此时最新数据本就是上一交易日的。
    """
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


def kline_is_stale(last_date: str | None, ts: datetime | None = None) -> bool:
    """K 线是否缺少最新交易日（比 `data_is_stale` 更严格）。

    场景：同花顺 / 部分兜底源的日线文件滞后一天（周五收盘后最新K线仍是周四、
    周一收盘后仍停在周五），此时 `data_is_stale` 的 3 天阈值恰好不触发，
    用户会静默看到缺最新一根的 K 线。这里在工作日收盘后要求 K 线包含今天：
    缺最近 1 个交易日（gap<=4，容忍跨周末）即判为滞后，长假（gap>4）仍不误报。
    ts 用于测试注入固定时间。
    """
    if not last_date:
        return False
    try:
        d = datetime.strptime(last_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    ts = ts or now()
    today = ts.date()
    if session_state(ts) == "closed" and is_weekday(today):
        gap = (today - d).days
        return 0 < gap <= 4
    return False
