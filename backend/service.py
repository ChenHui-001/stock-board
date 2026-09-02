"""业务编排层：拼装看板列表、详情页数据，统一走缓存。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from . import indicators, storage
from .cache import cache
from .config import settings
from .providers import Board, ProviderError, Quote, registry
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
            _fill_intraday_fields(quote)
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
    # get_quotes 已经走 _fill_intraday_fields；这里防御性再调一次，覆盖
    # force=True 时直接命中缓存但缓存里是旧对象的场景。
    return _fill_intraday_fields(quote)


def _fill_intraday_fields(quote: Quote) -> Quote:
    """P0-1：填充 VWAP 与现价偏离百分比。
    公式：vwap = quote.amount / quote.volume（金额元 / 成交量股，单位 元/股）。
    实测与 1 分钟线逐根累加偏差 <0.01%（000001 14:46 实测 11.8621 vs 真值 11.8618）。
    volume=0 时不计算（早盘集合竞价、未成交等场景），返回 None 而非除零错误。
    非交易时段：源返回的 amount/volume 是上一交易日累计值，VWAP 仍可计算，
    由上层用 quote.trade_date 判定是否需要"上一交易日"后缀提示。
    """
    try:
        vol = quote.volume
        amt = quote.amount
        if vol is not None and amt is not None and vol > 0 and amt > 0:
            vwap = round(amt / vol, 4)
            quote.vwap = vwap
            if quote.price and vwap:
                quote.deviation_pct = round((quote.price / vwap - 1) * 100, 3)
        else:
            quote.vwap = None
            quote.deviation_pct = None
    except (ZeroDivisionError, TypeError):
        quote.vwap = None
        quote.deviation_pct = None
    return quote


def _watch_monitor_flow(data: dict[str, Any], change: float,
                        flow: dict[str, Any] | None) -> dict[str, str] | None:
    """资金流驱动的监测信号(优先级高于量比信号)。

    flow 来自 indicators.summarize_flow 的输出,可用键:
      - available: bool    —— False 时本函数直接返回 None,让上层走量比逻辑
      - state: str         —— "主力抢筹" / "主力净流入" / "资金观望" / "主力净流出" / "主力出逃"
      - state_grade: str   —— "inflow" / "outflow" / "neutral"
      - main_last: float   —— 当日(或近5日回退)主力净额
      - streak: int        —— 连续同向天数
      - streak_dir: str    —— "流入" / "流出" / "持平"
      - fresh: bool        —— 是否当日数据(否则 reason 标注「近5日」)

    返回 dict[str, str] 给上层直接 return;返回 None 表示不触发资金流信号。
    """
    if not flow or not flow.get("available"):
        return None
    main_last = flow.get("main_last")
    state = flow.get("state") or ""
    state_grade = flow.get("state_grade") or ""
    streak = flow.get("streak") or 0
    streak_dir = flow.get("streak_dir") or ""
    fresh = bool(flow.get("fresh"))
    date_tag = "" if fresh else "（近5日）"

    has_main = isinstance(main_last, (int, float))
    main_wan = f"{main_last / 1e4:+.0f}万" if has_main else ""

    # ---- 主力抢筹 / 主力出货(信号最强,最先判断) ----
    # state="主力抢筹" = 主力连续 3 日流入 + 超大单主导(来自 _grade_flow_state)
    # 与"价"弱关联即可触发,因为资金动作本身就是强信号。
    if state.startswith("主力抢筹"):
        if change >= 0:
            return {
                "action": "主力抢筹",
                "tone": "up",
                "reason": (
                    f"{state}{date_tag}，主力资金持续入场"
                    + (f"，价 {change:+.2f}%" if change else "")
                ),
            }
        # 抢筹中但价格仍跌,标记"主力抢筹"提醒用户后续可能反转
        return {
            "action": "主力抢筹",
            "tone": "up",
            "reason": (
                f"{state}{date_tag}，价 {change:+.2f}% 暂时承压，"
                "关注能否企稳反弹"
            ),
        }

    if state.startswith("主力出逃"):
        if change <= 0:
            return {
                "action": "主力出货",
                "tone": "down",
                "reason": (
                    f"{state}{date_tag}，主力资金连续离场"
                    + (f"，价 {change:+.2f}%" if change else "")
                ),
            }
        return {
            "action": "主力出货",
            "tone": "down",
            "reason": (
                f"{state}{date_tag}，价 {change:+.2f}% 仍上行，"
                "警惕诱多风险"
            ),
        }

    # ---- 主力护盘: 价跌 ≥ 1% 但当日资金净流入(单日级别,不要求连续) ----
    # 优先于下面的「量价背离(洗盘)」——同样的条件,「护盘」是更积极正面描述。
    if has_main and change <= -1.0 and main_last > 0:
        return {
            "action": "主力护盘",
            "tone": "up",
            "reason": (
                f"跌 {change:+.2f}% 但主力净流入 {main_wan}{date_tag}，"
                "下方承接明显"
            ),
        }

    # ---- 量价背离: 价↑但资金出逃 OR 价↓但资金抢筹 ----
    # 用 0.5% 死区过滤极弱波动,避免把横盘误判成背离。
    # 必须放在 主力抢筹/出货/护盘 之后,否则价跌+资金流入/价涨+资金流出会被
    # 上面的强信号抢先命中(逻辑虽对但提示优先级不对)。
    if has_main and abs(change) >= 0.5:
        if change > 0 and main_last < -5e5:   # 价↑资金净流出 > 50 万
            return {
                "action": "量价背离",
                "tone": "warn",
                "reason": (
                    f"涨 {change:+.2f}% 但主力净流出 {main_wan}{date_tag}，"
                    "拉高出货风险"
                ),
            }
        if change < 0 and main_last > 5e5:    # 价↓资金净流入 > 50 万
            return {
                "action": "量价背离",
                "tone": "warn",
                "reason": (
                    f"跌 {change:+.2f}% 但主力净流入 {main_wan}{date_tag}，"
                    "可能为洗盘"
                ),
            }

    # ---- 持续流入 / 持续流出: 连 3 日同向(已经包含在抢筹/出逃里则不重复) ----
    # 仅对 state_grade 为 inflow/outflow 但 state 不是「抢筹/出逃」的中间档触发,
    # 比如「主力净流入」/「主力净流出」连续 3 日以上,说明趋势稳定。
    if streak >= 3 and state_grade == "inflow" and "抢筹" not in state:
        last5_wan = flow.get("main_last5", 0) / 1e4 if isinstance(flow.get("main_last5"), (int, float)) else 0
        return {
            "action": "持续流入",
            "tone": "up",
            "reason": (
                f"连 {streak} 日主力净流入{date_tag}，"
                f"近 5 日合计 {last5_wan:+.0f}万"
            ),
        }
    if streak >= 3 and state_grade == "outflow" and "出逃" not in state:
        last5_wan = flow.get("main_last5", 0) / 1e4 if isinstance(flow.get("main_last5"), (int, float)) else 0
        return {
            "action": "持续流出",
            "tone": "down",
            "reason": (
                f"连 {streak} 日主力净流出{date_tag}，"
                f"近 5 日合计 {last5_wan:+.0f}万"
            ),
        }

    return None


# ------------------------------------------------------------------ 自选股看板

def watch_monitor(data: dict[str, Any], atr: float | None = None,
                  flow: dict[str, Any] | None = None) -> dict[str, str]:
    """根据实时行情 + 资金流生成首页关键监测提示。

    v4 多维度策略（信号优先级从高到低）：
      1. 行情状态异常            → 继续观察(warn)
      2. 涨跌停                  → 涨停关注 / 跌停风险
      3. 量价背离                → 量价背离(warn)        [v4 新]
      4. 主力抢筹                → 主力抢筹(up)          [v4 新]
      5. 主力出货                → 主力出货(down)        [v4 新]
      6. 主力护盘                → 主力护盘(up)          [v4 新]
      7. 持续流入(连 3 日流入)     → 持续流入(up)          [v4 新]
      8. 持续流出(连 3 日流出)     → 持续流出(down)        [v4 新]
      9. 应减仓                  → 应减仓(down)          [v3]
      10. ATR 放量上涨           → 可加仓(up)            [v2]
      11. 放量上行(1%≤x<3% 量比≥2) → 放量上行(up)          [v3]
      12. 异动放量(量比 ≥ 3 方向不明) → 异动放量(warn)
      13. 高换手 ≥ 10%           → 高换手出货 / 高换手活跃
      14. 温和回调(-3<x≤-1.5% 量比≥1) → 温和回调(warn)      [v3]
      15. 流动性极低(换手 < 0.3%) → 流动性低(warn)
      16. 缩量阴跌               → 继续观察(地量阴跌)
      17. 偏弱趋势               → 谨慎持有
      18. 默认                   → 继续观察

    涨跌停限制按板块差异化（ST 5% / 创业板·科创板 20% / 北交所 30% / 主板 10%），
    容忍 ±0.3% 抖动。「可加仓」沿用 v2 的 ATR 归一化 + 量比过滤（无量拉升不算）。
    v4 起,资金流信号(via indicators.summarize_flow 的 state/state_grade/streak_dir 等)
    优先级高于量比信号——"谁在买"比"成交多不多"更早一步指示意图。
    flow=None 或 flow['available']=False 时,资金流信号全部跳过,向后兼容。
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

    # ---- 1.5. 资金流信号 (v4 新增) ----
    # 资金流向比"成交量"更早一步指示意图,优先级高于后续的量比信号。
    # 仅当 flow 存在且 available=True 时触发,否则跳过(向后兼容)。
    flow_signal = _watch_monitor_flow(data, change, flow)
    if flow_signal is not None:
        return flow_signal

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

    # ---- 3.5. 放量上行：涨幅 1% ≤ x < 3% 且量比 ≥ 2.0
    # 与「可加仓」(≥3% / ATR≥1.5) 错开 —— 中等涨幅配合显著量能,
    # 属于价量齐升但还没到加仓门槛的强势阶段。给绿色 vs 默认 flat 提高辨识度。
    if 1.0 <= change < 3.0 and has_vr and vr >= 2.0:
        return {
            "action": "放量上行",
            "tone": "up",
            "reason": (
                f"涨幅 {change:+.2f}% 且量比 {vr:.2f}，"
                "价量齐升，关注能否突破至加仓区间"
            ),
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

    # ---- 5.5. 温和回调：-3% < 跌幅 ≤ -1.5% 且量比 ≥ 1.0
    # 与「应减仓」(≤-3%) 和「谨慎持有」(量比<1.0) 错开 —— 量能未萎缩的浅回调
    # 往往只是洗盘而非趋势走坏,给一个温和告警避免被「应减仓」一刀切。
    # 优先级低于「异动放量」和「高换手」,后两者代表更紧迫的市场状态。
    if -3 < change <= -1.5 and has_vr and vr >= 1.0:
        return {
            "action": "温和回调",
            "tone": "warn",
            "reason": (
                f"跌幅 {change:+.2f}%{vr_text}，量能未萎缩，"
                "回调尚未恶化，关注量能是否进一步放大"
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
    rows = await storage.a_list_watchlist()
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

    # v4：并行预拉资金流,用于 watch_monitor 资金流信号(主力抢筹/出货/护盘/持续流入等)。
    # 复用详情页 flow 缓存(_flow 内部已有 history_ttl 缓存),失败回退 None
    # 让 watch_monitor 跳过资金流信号分支,不影响向后兼容。
    flow_by_key: dict[str, dict[str, Any] | None] = {}
    flow_results = await asyncio.gather(
        *(_flow(r["code"], r["market"], False) for r in rows),
        return_exceptions=True,
    )
    # 以 K 线最新交易日作为「资金流 fresh」参照,避免把昨日数据当今日
    ref_date_by_key: dict[str, str] = {
        full_code(r["code"], r["market"]): (kp.get("last_date") or "")
        for r, kp in zip(rows, kline_results)
        if not isinstance(kp, BaseException) and kp
    }
    for r, fp in zip(rows, flow_results):
        key = full_code(r["code"], r["market"])
        if isinstance(fp, BaseException) or not fp:
            flow_by_key[key] = None
            continue
        try:
            flow_by_key[key] = indicators.summarize_flow(
                fp.get("rows") or [],
                ref_date=ref_date_by_key.get(key) or None,
            )
        except Exception as exc:
            log.warning("watchlist 资金流汇总失败 %s: %s", key, exc)
            flow_by_key[key] = None

    # 板块兜底：腾讯/新浪行情不带行业字段，eastmoney 被限流时就会出现空板块。
    # 对缺失板块的股票用批量接口一次补齐（1 次请求覆盖多只，逐股 1 小时缓存），
    # 成功即回写数据库，之后即使 eastmoney 持续不可用也能稳定展示。
    missing: list[tuple[str, str]] = []
    for r in rows:
        quote = quotes.get(full_code(r["code"], r["market"]))
        if not (quote.board if quote else "") and not (r.get("board") or ""):
            missing.append((r["code"], r["market"]))
    board_map = await _industry_map(missing) if missing else {}

    # 回写攒批：原来在循环里逐行 update_meta，50 只自选股就是最多 100 次
    # 「抢锁 + 开事务 + commit 落 WAL」。攒成一批后只有 1 次。
    # 顺序与原来的逐条调用一致（同一行先回写名称、再回写板块），
    # COALESCE 语义不变，所以最终库状态与改之前逐条执行完全相同。
    pending_meta: list[tuple[str, str | None, str | None]] = []
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
                pending_meta.append((row["code"], quote.name, quote.board or None))
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
            pending_meta.append((row["code"], None, board))
        data["board"] = board
        data["monitor"] = watch_monitor(
            data, atr=atr_by_key.get(key), flow=flow_by_key.get(key),
        )
        data["sort_no"] = row["sort_no"]
        items.append(data)

    if pending_meta:
        await storage.a_update_meta_batch(pending_meta)

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

    # 与 kline_min / flow 等保持一致：K 线源挂掉时只丢 K 线，不再拖垮整个详情页
    # （空 bars 下游安全：build_ma / ATR / 支撑压力 / 震荡指标均按空序列处理）
    return await cached_pack(
        f"kline:{code}.{market}", history_ttl(), loader, force,
        empty={"bars": [], "source": ""},
    )


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


def _board_names(rows: list[Board]) -> list[str]:
    """P0-3 兼容：从 list[Board] 投影出纯名字列表，给老 API（添加自选）用。"""
    return [b.name for b in rows if b.name]


async def _boards(code: str, market: str, force: bool) -> list[Board]:
    """带结构的板块列表（1 小时缓存）。返回 list[Board]（含 code/market/name/change_pct），
    上层如需纯名字可用 _board_names() 投影（stock_detail 同时下发两条字段）。"""
    async def loader() -> Any:
        rows = await registry().boards(code, market)
        return {"rows": [b.to_dict() for b in rows]}

    pack = await cached_pack(
        f"boards:{code}.{market}", 3600.0, loader, force, empty={"rows": []}
    )
    rows_dict = pack.get("rows") or []
    valid_keys = set(Board.__annotations__.keys())
    return [Board(**{k: v for k, v in d.items() if k in valid_keys}) for d in rows_dict]


async def boards(code: str, market: str | None = None) -> list[str]:
    """公开的板块名列表（1 小时缓存，向后兼容老 API）。"""
    code = normalize_code(code)
    market = market or resolve_market(code)
    return _board_names(await _boards(code, market, False))


async def boards_detail(code: str, market: str | None = None) -> list[Board]:
    """公开的板块结构（1 小时缓存，含 code/market/name/change_pct）。P0-3 新接口。"""
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
        # boards 是 list[Board]，P0-3 后已升级；quote_dict["board"] 保留纯名字
        # （向下兼容：详情页 / watchlist 都按 str 处理），结构 走 boards_detail 字段
        first_name = boards[0].name if hasattr(boards[0], "name") else str(boards[0])
        quote_dict["board"] = first_name
        # 回写数据库，看板页从此不再依赖行情源是否携带行业字段
        await storage.a_update_meta(code, None, first_name)

    # 东财批量行情接口拿不到更新时间戳时（f86 语义异常），用 K 线最新日期回填，
    # 避免 trade_date 恒为空导致 delayed 判定与前端展示失效
    if not quote_dict.get("trade_date") and last_bar_date:
        quote_dict["trade_date"] = last_bar_date
    # data_is_stale 覆盖长假后仍停在节前的情况；kline_is_stale 覆盖同花顺/新浪这类
    # 日线源滞后一天（收盘后缺最新交易日）的情况，避免用户静默看到缺最新一根的K线
    if quote_dict["status"] == "normal" and (data_is_stale(last_bar_date) or kline_is_stale(last_bar_date)):
        quote_dict["status"] = "delayed"
        quote_dict["status_text"] = f"数据更新延迟（最新交易日 {last_bar_date}）"

    # P0-3：保留原 boards（纯名字列表，向后兼容）与新增 boards_detail（结构化）
    # 同时下发：老前端只看 boards；新前端用 boards_detail 拿板块代码/涨跌幅，
    # 用于走 secid=90.<code> 二次取板块行情，或直接读 change_pct 做情绪周期判定。
    boards_names = _board_names(boards)
    return {
        "quote": quote_dict,
        "boards": boards_names,
        "boards_detail": [b.to_dict() for b in boards],
        "watched": await storage.a_is_watched(code),
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
            # 取数失败且无历史数据可回退（只能给空结果）的数据源。
            # stale 分支前端已知「数据过期」，但 empty 分支连旧数据都没有且 stale=False，
            # 失败信息会静默丢失，因此单独上报一份，让前端能明确提示「该模块无数据」。
            #
            # 语义边界：这里只放「本该有数据、却连旧数据都没有」的源，不是「任何没取到的东西」。
            # 刻意不纳入的两类：
            #   - boards：板块为空常常是正常的（部分股票本就没有板块归类），进 errors 会误报；
            #     且 _boards 返回 list[Board] 而非 pack，error 在转换时就丢了，接进来要动数据层。
            #   - kline_min：P2-7 的增强项（60 分钟线画当日走势），缺了只是图表降级；
            #     且 _kline 已占「K线」这个显示名，再加一条同名会让用户分不清是哪一路。
            "errors": [
                name for name, pack in
                (("K线", kline_pack), ("资金流向", flow_pack), ("两融", margin_pack), ("财报", financial_pack))
                if pack.get("error") and not pack.get("stale")
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
    watched = await storage.a_watched_codes()
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
    watched = await storage.a_watched_codes()

    def pack_rows(items: list[Quote]) -> list[dict[str, Any]]:
        return [{**q.to_dict(), "watched": q.code in watched} for q in items]

    return {
        **{k: pack_rows(v) for k, v in data.items()},
        "stale": pack.get("stale", False),
        "error": pack.get("error", ""),
    }

