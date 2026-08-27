"""业务编排层：拼装看板列表、详情页数据，统一走缓存。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from . import indicators, storage
from .cache import cache
from .config import settings
from .providers import ProviderError, Quote, registry
from .utils import (
    data_is_stale,
    describe_exc,
    full_code,
    is_trading_now,
    kline_is_stale,
    normalize_code,
    resolve_market,
    session_state,
)

log = logging.getLogger("service")


def quote_ttl() -> float:
    return settings.QUOTE_TTL_OPEN if is_trading_now() else settings.QUOTE_TTL_CLOSED


def history_ttl() -> float:
    return settings.HISTORY_TTL_OPEN if is_trading_now() else settings.HISTORY_TTL_CLOSED


# 上一次成功的数据保留 24 小时。数据源被频控/宕机时，宁可展示带「延迟」标记的
# 旧数据，也不要给用户一片空白（K线、资金、两融都是日频数据，隔夜可用）。
STALE_TTL = 86400.0
FINANCIAL_TTL = 21600.0  # 定期报告发布频率低，网页数据 6 小时内可复用


async def cached_pack(
    key: str,
    ttl: float,
    loader: Any,
    force: bool,
    *,
    empty: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """带「过期兜底」的缓存读取：取数失败时回退到最近一次成功的结果。"""
    try:
        data = await cache.get_or_set(key, ttl, loader, force=force)
        cache.put(f"stale:{key}", data, STALE_TTL)
        return {**data, "stale": False}
    except Exception as exc:  # noqa: BLE001
        stale = cache.peek(f"stale:{key}")
        if stale is not None:
            log.info("%s 取数失败，回退上一次成功数据：%s", key, describe_exc(exc))
            return {**stale, "stale": True, "error": describe_exc(exc)}
        log.info("%s 取数失败且无历史数据：%s", key, describe_exc(exc))
        if empty is None:
            raise
        return {**empty, "stale": False, "error": describe_exc(exc)}


# ------------------------------------------------------------------ 行情

async def get_quotes(keys: list[tuple[str, str]], force: bool = False) -> dict[str, Quote]:
    """带缓存的批量行情。缓存粒度是单只股票，便于不同页面复用。"""
    ttl = quote_ttl()
    result: dict[str, Quote] = {}
    missing: list[tuple[str, str]] = []

    for code, market in keys:
        code = normalize_code(code)
        market = market or resolve_market(code)
        key = f"{code}.{market}"
        hit = None if force else cache.peek(f"quote:{key}")
        if hit is not None:
            result[key] = hit
        else:
            missing.append((code, market))

    if missing:
        fetched, _ = await registry().quotes(missing)
        for key, quote in fetched.items():
            cache.put(f"quote:{key}", quote, ttl)
            result[key] = quote
    return result


async def get_quote(code: str, market: str | None = None, force: bool = False) -> Quote:
    code = normalize_code(code)
    market = market or resolve_market(code)
    data = await get_quotes([(code, market)], force=force)
    quote = data.get(f"{code}.{market}")
    if not quote:
        raise ProviderError(f"未获取到 {code} 的行情")
    return quote


# ------------------------------------------------------------------ 自选股看板

def watch_monitor(data: dict[str, Any], atr: float | None = None) -> dict[str, str]:
    """根据实时行情生成首页关键监测提示。

    v3 多维度策略（信号优先级从高到低）：
      1. 行情状态异常            → 继续观察(warn)
      2. 涨跌停                  → 涨停关注 / 跌停风险
      3. 放量下跌                → 应减仓
      4. ATR 放量上涨            → 可加仓
      5. 异动放量(量比 ≥ 3 方向不明) → 异动放量
      6. 高换手 ≥ 10%            → 高换手出货 / 高换手活跃
      7. 流动性极低(换手 < 0.3%) → 流动性低
      8. 缩量阴跌                → 继续观察(地量阴跌)
      9. 偏弱趋势                → 谨慎持有
      10. 默认                   → 继续观察

    涨跌停限制按板块差异化（ST 5% / 创业板·科创板 20% / 北交所 30% / 主板 10%），
    容忍 ±0.3% 抖动。「可加仓」沿用 v2 的 ATR 归一化 + 量比过滤（无量拉升不算）。
    """
    status = data.get("status") or "unknown"
    if status != "normal":
        return {
            "action": "继续观察",
            "tone": "warn",
            "reason": data.get("status_text") or "行情状态异常，暂不判断",
        }

    change = data.get("change_pct")
    if not isinstance(change, (int, float)):
        return {"action": "继续观察", "tone": "flat", "reason": "涨跌幅暂无数据"}

    vr = data.get("volume_ratio")
    turnover = data.get("turnover")
    has_vr = isinstance(vr, (int, float))
    has_to = isinstance(turnover, (int, float))
    vr_text = f"，量比 {vr:.2f}" if has_vr else ""
    to_text = f"，换手 {turnover:.2f}%" if has_to else ""

    # ---- 1. 涨跌停：板块差异化 + ±0.3% 抖动容忍
    limit_pct = _limit_pct(data.get("market"), data.get("code"), data.get("name"))
    near_limit = limit_pct - 0.3
    if change >= near_limit:
        return {
            "action": "涨停关注",
            "tone": "warn",
            "reason": (
                f"涨幅 {change:+.2f}%，触及涨停 ±{limit_pct:.0f}%，"
                "次日溢价/分歧需关注"
            ),
        }
    if change <= -near_limit:
        return {
            "action": "跌停风险",
            "tone": "down",
            "reason": (
                f"跌幅 {change:+.2f}%，触及跌停 ∓{limit_pct:.0f}%，"
                "次日可能继续下挫"
            ),
        }

    # ---- 2. 应减仓：跌幅 ≤ -3% 且量比 ≥ 0.8（缩量阴跌属空头衰竭，不触发减仓）
    if change <= -3 and (not has_vr or vr >= 0.8):
        return {
            "action": "应减仓",
            "tone": "down",
            "reason": f"跌幅 {change:+.2f}%{vr_text}，短线风险升高",
        }

    # ---- 3. 可加仓：ATR 归一化或固定 3% 兜底 + 量比 ≥ 1.5
    price = data.get("price")
    if isinstance(atr, (int, float)) and atr > 0 \
            and isinstance(price, (int, float)) and price > 0:
        atr_pct = atr / price * 100
        unit_atr = abs(change) / atr_pct if atr_pct > 0 else 0
        if change >= 1 and unit_atr >= 1.5 and has_vr and vr >= 1.5:
            return {
                "action": "可加仓",
                "tone": "up",
                "reason": (
                    f"涨幅 {change:+.2f}% ≈ {unit_atr:.1f} 倍 ATR，"
                    f"量比 {vr:.2f}，动能较强"
                ),
            }
    elif change >= 3 and has_vr and vr >= 1.5:
        return {
            "action": "可加仓",
            "tone": "up",
            "reason": f"涨幅 {change:+.2f}% 且量比 {vr:.2f}，动能较强",
        }

    # ---- 4. 异动放量：量比 ≥ 3 但方向不明(|change| < 2%)
    if has_vr and vr >= 3 and abs(change) < 2:
        return {
            "action": "异动放量",
            "tone": "warn",
            "reason": (
                f"量比 {vr:.2f} 但涨跌幅仅 {change:+.2f}%，"
                "方向不明，关注后续突破/跌破"
            ),
        }

    # ---- 5. 高换手(≥ 10%)：按方向区分出货 / 活跃
    if has_to and turnover >= 10:
        if change < 0:
            return {
                "action": "高换手出货",
                "tone": "down",
                "reason": (
                    f"换手率 {turnover:.1f}% 且下跌 {change:+.2f}%，"
                    "资金分歧出货"
                ),
            }
        return {
            "action": "高换手活跃",
            "tone": "warn",
            "reason": (
                f"换手率 {turnover:.1f}% 且上涨 {change:+.2f}%，"
                "交投活跃，关注持续性"
            ),
        }

    # ---- 6. 流动性极低：换手 < 0.3%（数据缺失时 is_num 为 False，不会误报）
    if has_to and turnover < 0.3:
        return {
            "action": "流动性低",
            "tone": "warn",
            "reason": (
                f"换手率仅 {turnover:.2f}%，流动性极差，"
                "价格易被小额单子砸动"
            ),
        }

    # ---- 7. 缩量阴跌：跌幅 ≤ -3% 但量比 < 0.8（地量阴跌 → 关注企稳）
    if change <= -3 and has_vr and vr < 0.8:
        return {
            "action": "继续观察",
            "tone": "flat",
            "reason": (
                f"跌幅 {change:+.2f}% 但量比仅 {vr:.2f}，"
                "地量阴跌，关注是否企稳"
            ),
        }

    # ---- 8. 偏弱趋势：跌幅 ≤ -1.5% 且量能不足（未触发减仓）
    if change <= -1.5 and (not has_vr or vr < 1.0):
        return {
            "action": "谨慎持有",
            "tone": "flat",
            "reason": (
                f"跌幅 {change:+.2f}%{vr_text}，动能偏弱，"
                "未破位前继续持有"
            ),
        }

    # ---- 9. 默认
    return {
        "action": "继续观察",
        "tone": "flat",
        "reason": f"涨跌幅 {change:+.2f}%{vr_text}{to_text}，未形成明确信号",
    }


def _limit_pct(market: str | None, code: str | None, name: str | None) -> float:
    """估算个股日涨跌幅限制(%)。

    - ST/*ST: 5%
    - 创业板(300/301): 20%
    - 科创板(688): 20%
    - 北交所(market=BJ 或 8/4 开头): 30%
    - 其它主板/中小板: 10%
    """
    nm = name or ""
    if "ST" in nm.upper():
        return 5.0
    c = code or ""
    if c.startswith(("300", "301")):
        return 20.0
    if c.startswith("688"):
        return 20.0
    m = (market or "").upper()
    if m in ("BJ", "BSE"):
        return 30.0
    if c.startswith(("83", "87", "43", "82")):
        return 30.0
    return 10.0


async def watchlist_board(force: bool = False) -> dict[str, Any]:
    rows = storage.list_watchlist()
    if not rows:
        return {"items": [], "session": session_info()}

    keys = [(r["code"], r["market"]) for r in rows]
    try:
        quotes = await get_quotes(keys, force=force)
    except ProviderError as exc:
        log.warning("看板行情获取失败：%s", exc)
        quotes = {}

    # 并行预拉 K 线算 ATR：复用详情页 kline 缓存（首页请求触发的也作为预热），
    # K 线拉取失败不会阻塞 monitor 判定——会退回到固定 3% 门槛
    atr_by_key: dict[str, float | None] = {}
    kline_results = await asyncio.gather(
        *(_kline(r["code"], r["market"], False) for r in rows),
        return_exceptions=True,
    )
    for r, kp in zip(rows, kline_results):
        key = full_code(r["code"], r["market"])
        if isinstance(kp, BaseException) or not kp:
            atr_by_key[key] = None
            continue
        bars = kp.get("bars") or []
        atr_by_key[key] = indicators.decorate_bars_with_atr(bars)

    # 板块兜底：腾讯/新浪行情不带行业字段，eastmoney 被限流时就会出现空板块。
    # 对缺失板块的股票用批量接口一次补齐（1 次请求覆盖多只，逐股 1 小时缓存），
    # 成功即回写数据库，之后即使 eastmoney 持续不可用也能稳定展示。
    missing: list[tuple[str, str]] = []
    for r in rows:
        quote = quotes.get(full_code(r["code"], r["market"]))
        if not (quote.board if quote else "") and not (r.get("board") or ""):
            missing.append((r["code"], r["market"]))
    board_map = await _industry_map(missing) if missing else {}

    items: list[dict[str, Any]] = []
    for row in rows:
        key = full_code(row["code"], row["market"])
        quote = quotes.get(key)
        if quote:
            data = quote.to_dict()
            # 板块以数据源为准，源没给就用入库时记录的
            if not data.get("board"):
                data["board"] = row.get("board") or ""
            if not data.get("name"):
                data["name"] = row.get("name") or ""
            if quote.name and quote.name != row.get("name"):
                storage.update_meta(row["code"], quote.name, quote.board or None)
        else:
            data = Quote(
                code=row["code"],
                market=row["market"],
                name=row.get("name") or row["code"],
                board=row.get("board") or "",
                status="unknown",
                status_text="数据更新延迟，暂无有效行情",
            ).to_dict()
        board = (data.get("board") or "").strip() or board_map.get(key, "")
        if board and board != row.get("board"):
            storage.update_meta(row["code"], None, board)
        data["board"] = board
        data["monitor"] = watch_monitor(data, atr=atr_by_key.get(key))
        data["sort_no"] = row["sort_no"]
        items.append(data)

    return {"items": items, "session": session_info()}


async def _industry_map(keys: list[tuple[str, str]]) -> dict[str, str]:
    """补齐自选股所属行业：批量接口一次覆盖多只（1 小时缓存），

    批量接口被 CDN 限流时回退到逐股 slist 查询（同样 1 小时缓存）。
    """
    found: dict[str, str] = {}
    missing: list[tuple[str, str]] = []
    for code, market in keys:
        key = full_code(code, market)
        hit = cache.peek(f"industry:{key}")
        if hit:
            found[key] = hit
        else:
            missing.append((code, market))
    if missing:
        try:
            fetched = await registry().industry(missing)
        except ProviderError as exc:
            log.info("行业批量查询失败：%s", exc)
            fetched = {}
        for key, name in fetched.items():
            cache.put(f"industry:{key}", name, 3600.0)
            found[key] = name
        # 批量接口没覆盖到的（失败/空结果），并发逐股兜底，避免串行拖慢看板接口
        still = [(c, m) for c, m in missing if full_code(c, m) not in found]
        if still:
            async def _fetch_board(item: tuple[str, str]) -> tuple[str, str, str | None]:
                code, market = item
                names = await _boards(code, market, False)
                return code, market, (names[0] if names else None)

            results = await asyncio.gather(
                *(_fetch_board(item) for item in still), return_exceptions=True
            )
            for r in results:
                if isinstance(r, BaseException):
                    log.info("逐股板块兜底失败：%s", r)
                    continue
                code, market, name = r
                if name:
                    key = full_code(code, market)
                    cache.put(f"industry:{key}", name, 3600.0)
                    found[key] = name
    return found


def session_info() -> dict[str, Any]:
    state = session_state()
    trading = is_trading_now()
    return {
        "state": state,
        "trading": trading,
        "auto_refresh": trading,
        "interval_ms": 5000 if trading else 0,
        "label": {
            "open": "盘中交易",
            "lunch_break": "午间休市",
            "pre_open": "尚未开盘",
            "closed": "已收盘",
            "weekend": "休市中",
        }.get(state, state),
    }


# ------------------------------------------------------------------ 详情页

async def _kline(code: str, market: str, force: bool) -> dict[str, Any]:
    async def loader() -> Any:
        bars, src = await registry().kline(code, market, settings.KLINE_LIMIT)
        return {"bars": bars, "source": src}

    return await cached_pack(f"kline:{code}.{market}", history_ttl(), loader, force)


def _kline_min_ttl() -> float:
    """P2-7：分钟 K 线缓存 TTL。盘中 30s 实时，盘后 10 分钟历史数据基本不变。"""
    return 30.0 if is_trading_now() else 600.0


async def _kline_min(code: str, market: str, force: bool) -> dict[str, Any]:
    """60 分钟 K 线（最近 24 根 = 10 个交易日 × 4 时段），仅盘中拉取时延展到位。

    非盘中时段仍可拉（东财保留历史分钟数据），用于给用户看"今日累计走势"。
    """
    async def loader() -> Any:
        bars, src = await registry().kline_min(code, market, 24, klt=60)
        return {"bars": bars, "source": src}

    return await cached_pack(
        f"kline60:{code}.{market}", _kline_min_ttl(), loader, force,
        empty={"bars": [], "source": ""},
    )


async def _flow(code: str, market: str, force: bool) -> dict[str, Any]:
    async def loader() -> Any:
        rows, src = await registry().fund_flow(code, market, settings.FLOW_DAYS)
        return {"rows": rows, "source": src}

    return await cached_pack(
        f"flow:{code}.{market}", history_ttl(), loader, force,
        empty={"rows": [], "source": ""},
    )


async def _margin(code: str, market: str, force: bool) -> dict[str, Any]:
    async def loader() -> Any:
        rows, src = await registry().margin(code, market, settings.MARGIN_DAYS)
        return {"rows": rows, "source": src}

    return await cached_pack(
        f"margin:{code}.{market}", history_ttl(), loader, force,
        empty={"rows": [], "source": ""},
    )


async def _financials(code: str, market: str, force: bool) -> dict[str, Any]:
    async def loader() -> Any:
        rows, src = await registry().financials(code, market, 12)
        return {"rows": rows, "source": src}

    return await cached_pack(
        f"financials:{code}.{market}", FINANCIAL_TTL, loader, force,
        empty={"rows": [], "source": ""},
    )


async def _boards(code: str, market: str, force: bool) -> list[str]:
    async def loader() -> Any:
        return {"names": await registry().boards(code, market)}

    pack = await cached_pack(
        f"boards:{code}.{market}", 3600.0, loader, force, empty={"names": []}
    )
    return [n for n in (pack.get("names") or []) if n]


async def boards(code: str, market: str | None = None) -> list[str]:
    """公开的板块查询（1 小时缓存），用于添加自选等轻量场景。"""
    code = normalize_code(code)
    market = market or resolve_market(code)
    return await _boards(code, market, False)


async def stock_detail(code: str, market: str | None = None, force: bool = False) -> dict[str, Any]:
    code = normalize_code(code)
    market = market or resolve_market(code)

    # 并发取数；任一失败立即取消其余任务，避免孤儿协程继续打上游接口
    # （FIRST_EXCEPTION：无异常时等全部完成，有异常时立刻返回并取消未完成的）
    tasks = {
        "quote": asyncio.create_task(get_quote(code, market, force=force)),
        "kline": asyncio.create_task(_kline(code, market, force)),
        # P2-7：盘中拉取 60 分钟 K 线；任一源失败即降级返回空 bars
        "kline_min": asyncio.create_task(_kline_min(code, market, force)),
        "flow": asyncio.create_task(_flow(code, market, force)),
        "margin": asyncio.create_task(_margin(code, market, force)),
        "financials": asyncio.create_task(_financials(code, market, force)),
        "boards": asyncio.create_task(_boards(code, market, force)),
    }
    done, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_EXCEPTION)
    for t in done:
        exc = t.exception()
        if exc is not None:
            for p in pending:
                p.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise exc

    quote = tasks["quote"].result()
    kline_pack = tasks["kline"].result()
    kline_min_pack = tasks["kline_min"].result()
    flow_pack = tasks["flow"].result()
    margin_pack = tasks["margin"].result()
    financial_pack = tasks["financials"].result()
    boards = tasks["boards"].result()

    bars = kline_pack["bars"]
    ma_infos, ma_summary = indicators.build_ma(bars, quote.price)
    ma_values = {i.window: i.value for i in ma_infos}
    # ATR(14)：把每根 Bar.atr 写满，返回最后一根的 ATR 给支撑压力/趋势判定用
    last_atr = indicators.decorate_bars_with_atr(bars)
    sr = indicators.support_resistance(bars, quote.price, ma_values, atr=last_atr)
    # P2-7：盘中 60 分钟趋势（数据不足/源不支持时 available=False）
    intraday = indicators.intraday_trend_state(kline_min_pack.get("bars") or [])
    last_bar_date = bars[-1].date if bars else ""
    # ref_date=K线最新日期：资金流向当日数据未发布（盘中/16点前）时
    # summarize_flow 据此降级判定并标注，避免把昨日数据当「当日」
    flow_summary = indicators.summarize_flow(flow_pack["rows"], ref_date=last_bar_date)
    margin_summary = indicators.summarize_margin(margin_pack["rows"])
    # P0-1/P0-3：盘前 9:30 之前所有数据类标签统一降级为"待开盘"；
    # P0-3 数据延迟时给所有标签 warn 染色避免静默误导。
    # 两者都先暂存 quote_dict，再把最终延迟判定（基于 last_bar_date）传进 build_status
    delayed_for_status = data_is_stale(last_bar_date) or kline_is_stale(last_bar_date)
    pre_open_for_status = session_state() == "pre_open"
    status = indicators.build_status(
        quote, bars, flow_summary, margin_summary, ma_summary, sr,
        atr=last_atr,
        pre_open=pre_open_for_status,
        delayed=delayed_for_status,
        intraday=intraday,
    )
    osc = indicators.compute_oscillators(bars)

    quote_dict = quote.to_dict()
    if not quote_dict.get("board") and boards:
        quote_dict["board"] = boards[0]
        # 回写数据库，看板页从此不再依赖行情源是否携带行业字段
        storage.update_meta(code, None, boards[0])

    # 东财批量行情接口拿不到更新时间戳时（f86 语义异常），用 K 线最新日期回填，
    # 避免 trade_date 恒为空导致 delayed 判定与前端展示失效
    if not quote_dict.get("trade_date") and last_bar_date:
        quote_dict["trade_date"] = last_bar_date
    # data_is_stale 覆盖长假后仍停在节前的情况；kline_is_stale 覆盖同花顺/新浪这类
    # 日线源滞后一天（收盘后缺最新交易日）的情况，避免用户静默看到缺最新一根的K线
    if quote_dict["status"] == "normal" and (data_is_stale(last_bar_date) or kline_is_stale(last_bar_date)):
        quote_dict["status"] = "delayed"
        quote_dict["status_text"] = f"数据更新延迟（最新交易日 {last_bar_date}）"

    return {
        "quote": quote_dict,
        "boards": boards,
        "watched": storage.is_watched(code),
        "kline": [asdict(b) for b in bars[-120:]],
        "ma": [asdict(m) for m in ma_infos],
        "ma_summary": {
            **ma_summary,
            "series": {
                name: seq[-120:] for name, seq in ma_summary["series"].items()
            },
        },
        "support_resistance": sr,
        "oscillators": osc,
        "fund_flow": {
            "rows": [asdict(r) for r in flow_pack["rows"]],
            "summary": flow_summary,
            "source": flow_pack.get("source", ""),
            "stale": flow_pack.get("stale", False),
            "error": flow_pack.get("error", ""),
        },
        "margin": {
            "rows": [asdict(r) for r in margin_pack["rows"]],
            "summary": margin_summary,
            "source": margin_pack.get("source", ""),
            "stale": margin_pack.get("stale", False),
            "error": margin_pack.get("error", ""),
        },
        "financials": {
            "rows": [asdict(r) for r in financial_pack["rows"]],
            "source": financial_pack.get("source", ""),
            "stale": financial_pack.get("stale", False),
            "error": financial_pack.get("error", ""),
        },
        "status": status,
        "sources": {
            "quote": quote.source,
            "kline": kline_pack.get("source", ""),
            "kline_min": kline_min_pack.get("source", ""),
            "fund_flow": flow_pack.get("source", ""),
            "margin": margin_pack.get("source", ""),
            "financials": financial_pack.get("source", ""),
            "stale": [
                name for name, pack in
                (("K线", kline_pack), ("资金流向", flow_pack), ("两融", margin_pack), ("财报", financial_pack))
                if pack.get("stale")
            ],
        },
        # P2-7：60 分钟 K 线原始 bars（用于前端画当日实时走势）
        "kline_min": [asdict(b) for b in (kline_min_pack.get("bars") or [])[-24:]],
        "session": session_info(),
    }


# ------------------------------------------------------------------ 搜索 / 热门

async def search(keyword: str, limit: int = 15) -> list[dict[str, Any]]:
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    async def loader() -> Any:
        return await registry().search(keyword, limit)

    try:
        items = await cache.get_or_set(
            f"search:{keyword.lower()}:{limit}", settings.SEARCH_TTL, loader
        )
    except ProviderError as exc:
        log.info("搜索失败 %s: %s", keyword, exc)
        return []

    keys = [(i.code, i.market) for i in items]
    try:
        quotes = await get_quotes(keys)
    except ProviderError:
        quotes = {}

    out: list[dict[str, Any]] = []
    watched = storage.watched_codes()
    for item in items:
        key = f"{item.code}.{item.market}"
        quote = quotes.get(key)
        row: dict[str, Any] = {
            "code": item.code,
            "market": item.market,
            "name": item.name,
            "board": (quote.board if quote else "") or item.board,
            "price": quote.price if quote else None,
            "prev_close": quote.prev_close if quote else None,
            "change_pct": quote.change_pct if quote else None,
            "status": quote.status if quote else "unknown",
            "status_text": quote.status_text if quote else "",
            "watched": item.code in watched,
        }
        out.append(row)
    return out


async def hot(limit: int = 8) -> dict[str, Any]:
    async def loader() -> Any:
        return {"data": await registry().hot(limit)}

    pack = await cached_pack(
        f"hot:{limit}", settings.HOT_TTL, loader, False,
        empty={"data": {"gainers": [], "losers": [], "actives": []}},
    )
    data = pack.get("data") or {}
    watched = storage.watched_codes()

    def pack_rows(items: list[Quote]) -> list[dict[str, Any]]:
        return [{**q.to_dict(), "watched": q.code in watched} for q in items]

    return {
        **{k: pack_rows(v) for k, v in data.items()},
        "stale": pack.get("stale", False),
        "error": pack.get("error", ""),
    }

