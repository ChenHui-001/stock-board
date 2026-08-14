"""技术指标与状态标签引擎。

产出的结构同时服务于：详情页「均线模块 / 状态模块」、AI 分析的输入投喂、
以及无 LLM 时的规则降级分析。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .providers import Bar, FlowDay, MarginDay, Quote

MA_WINDOWS = (5, 10, 20, 60)


# ------------------------------------------------------------------ 均线

def moving_average(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= window:
            total -= values[i - window]
        out.append(round(total / window, 3) if i >= window - 1 else None)
    return out


def ma_series(bars: Sequence[Bar]) -> dict[int, list[float | None]]:
    closes = [b.close for b in bars]
    return {w: moving_average(closes, w) for w in MA_WINDOWS}


def _slope_state(series: Sequence[float | None], lookback: int = 5) -> tuple[str, float | None]:
    """均线自身的走向：上行 / 走平 / 下行，并返回区间变动百分比。"""
    valid = [v for v in series if v is not None]
    if len(valid) < lookback + 1:
        return "数据不足", None
    now, before = valid[-1], valid[-1 - lookback]
    if not before:
        return "数据不足", None
    delta = (now - before) / before * 100
    if delta > 0.6:
        state = "上行"
    elif delta < -0.6:
        state = "下行"
    else:
        state = "走平"
    return state, round(delta, 2)


@dataclass
class MaInfo:
    window: int
    value: float | None
    slope: str
    slope_pct: float | None
    position: str          # 站上 / 跌破 / 贴合
    deviation_pct: float | None  # 股价相对均线的乖离率


def build_ma(bars: Sequence[Bar], price: float | None) -> tuple[list[MaInfo], dict[str, Any]]:
    series = ma_series(bars)
    infos: list[MaInfo] = []
    for w in MA_WINDOWS:
        seq = series[w]
        value = seq[-1] if seq else None
        slope, slope_pct = _slope_state(seq)
        position, deviation = "数据不足", None
        if value and price:
            deviation = round((price - value) / value * 100, 2)
            if deviation > 0.5:
                position = "站上"
            elif deviation < -0.5:
                position = "跌破"
            else:
                position = "贴合"
        infos.append(MaInfo(w, value, slope, slope_pct, position, deviation))

    values = {i.window: i.value for i in infos}
    arrangement = "均线交织"
    m5, m10, m20, m60 = (values.get(w) for w in MA_WINDOWS)
    if None not in (m5, m10, m20, m60):
        if m5 > m10 > m20 > m60:  # type: ignore[operator]
            arrangement = "多头排列"
        elif m5 < m10 < m20 < m60:  # type: ignore[operator]
            arrangement = "空头排列"
        elif m5 > m10 > m20:  # type: ignore[operator]
            arrangement = "短期多头"
        elif m5 < m10 < m20:  # type: ignore[operator]
            arrangement = "短期空头"

    above = [i.window for i in infos if i.position == "站上"]
    below = [i.window for i in infos if i.position == "跌破"]
    summary = {
        "arrangement": arrangement,
        "above": above,
        "below": below,
        "above_count": len(above),
        "series": {f"MA{w}": series[w] for w in MA_WINDOWS},
    }
    return infos, summary


# ------------------------------------------------------------------ 支撑压力

def support_resistance(bars: Sequence[Bar], price: float | None, ma_values: dict[int, float | None]) -> dict[str, Any]:
    """用近 20/60 日高低点 + 均线，取最近的下方支撑与上方压力。"""
    result: dict[str, Any] = {
        "support": None, "resistance": None,
        "support_from": "", "resistance_from": "",
        "high_20": None, "low_20": None, "high_60": None, "low_60": None,
        "state": "数据不足",
    }
    if not bars or not price:
        return result

    recent20 = bars[-20:]
    recent60 = bars[-60:]
    high20 = max(b.high for b in recent20)
    low20 = min(b.low for b in recent20)
    high60 = max(b.high for b in recent60)
    low60 = min(b.low for b in recent60)
    result.update(high_20=round(high20, 2), low_20=round(low20, 2),
                  high_60=round(high60, 2), low_60=round(low60, 2))

    candidates: list[tuple[float, str]] = [
        (high20, "20日高点"), (low20, "20日低点"),
        (high60, "60日高点"), (low60, "60日低点"),
    ]
    for w, v in ma_values.items():
        if v:
            candidates.append((v, f"MA{w}"))

    below = [(v, tag) for v, tag in candidates if v < price * 0.999]
    above = [(v, tag) for v, tag in candidates if v > price * 1.001]
    if below:
        v, tag = max(below, key=lambda x: x[0])
        result["support"], result["support_from"] = round(v, 2), tag
    if above:
        v, tag = min(above, key=lambda x: x[0])
        result["resistance"], result["resistance_from"] = round(v, 2), tag

    # 区间位置判定
    if price >= high20 * 0.995:
        result["state"] = "突破区间上沿" if price >= high20 else "逼近压力位"
    elif price <= low20 * 1.005:
        result["state"] = "跌破区间下沿" if price <= low20 else "逼近支撑位"
    else:
        span = high20 - low20
        pos = (price - low20) / span if span else 0.5
        if pos > 0.66:
            result["state"] = "运行于区间上半区"
        elif pos < 0.33:
            result["state"] = "运行于区间下半区"
        else:
            result["state"] = "区间中枢震荡"
    result["range_pos_pct"] = (
        round((price - low20) / (high20 - low20) * 100, 1) if high20 > low20 else None
    )
    return result


# ------------------------------------------------------------------ 资金流向

def summarize_flow(rows: Sequence[FlowDay]) -> dict[str, Any]:
    if not rows:
        return {"available": False, "trend": "无数据", "days": 0}

    main_total = sum(r.main for r in rows)
    xl_total = sum(r.xl for r in rows)
    lg_total = sum(r.lg for r in rows)
    md_total = sum(r.md for r in rows)
    sm_total = sum(r.sm for r in rows)
    inflow_days = sum(1 for r in rows if r.main > 0)
    outflow_days = len(rows) - inflow_days

    # 连续同向天数（从最后一天往前数）
    streak, direction = 0, 0
    for r in reversed(rows):
        cur = 1 if r.main > 0 else (-1 if r.main < 0 else 0)
        if cur == 0:
            break
        if direction == 0:
            direction = cur
        if cur != direction:
            break
        streak += 1

    last5 = sum(r.main for r in rows[-5:])
    ratio = inflow_days / len(rows)

    # 备用源（新浪）只有「净流入」与「超大单」两档，没有大单/中单拆分。
    # 这里识别出来，避免把不存在的口径当成真实数据展示或投喂给 AI。
    tiered = any(r.lg or r.md for r in rows)
    if main_total > 0 and ratio >= 0.6:
        trend = "持续流入"
    elif main_total < 0 and ratio <= 0.4:
        trend = "持续流出"
    elif main_total > 0:
        trend = "震荡偏流入"
    elif main_total < 0:
        trend = "震荡偏流出"
    else:
        trend = "震荡反复"

    if main_total > 0 and last5 > 0:
        state = "主力净流入"
    elif main_total < 0 and last5 < 0:
        state = "主力净流出"
    else:
        state = "资金观望"

    return {
        "available": True,
        "tiered": tiered,
        "days": len(rows),
        "main_total": round(main_total, 2),
        "xl_total": round(xl_total, 2),
        "lg_total": round(lg_total, 2) if tiered else None,
        "md_total": round(md_total, 2) if tiered else None,
        "sm_total": round(sm_total, 2) if tiered else None,
        "main_last": round(rows[-1].main, 2),
        "main_last5": round(last5, 2),
        "inflow_days": inflow_days,
        "outflow_days": outflow_days,
        "streak": streak,
        "streak_dir": "流入" if direction > 0 else ("流出" if direction < 0 else "持平"),
        "trend": trend,
        "state": state,
    }


# ------------------------------------------------------------------ 两融

def summarize_margin(rows: Sequence[MarginDay]) -> dict[str, Any]:
    if not rows:
        return {"available": False, "sentiment": "无两融数据", "days": 0}

    first = rows[0]
    last = rows[-1]
    rz_change = None
    rz_change_pct = None
    if first.rzye and last.rzye:
        rz_change = round(last.rzye - first.rzye, 2)
        rz_change_pct = round((last.rzye - first.rzye) / first.rzye * 100, 2)

    rq_change = None
    if first.rqye is not None and last.rqye is not None:
        rq_change = round(last.rqye - first.rqye, 2)

    rz_net = round(sum(r.rzjme or 0 for r in rows), 2)
    rz_buy = round(sum(r.rzmre or 0 for r in rows), 2)
    rq_sell = round(sum(r.rqmcl or 0 for r in rows), 2)

    if rz_change_pct is not None and rz_change_pct >= 5 and rz_net > 0:
        sentiment = "融资做多情绪旺盛"
    elif rz_change_pct is not None and rz_change_pct <= -5:
        sentiment = "融资资金撤离"
    elif rq_change is not None and last.rqye and first.rqye and last.rqye > first.rqye * 1.3:
        sentiment = "融券做空情绪浓厚"
    else:
        sentiment = "两融情绪平稳"

    return {
        "available": True,
        "days": len(rows),
        "rzye_last": last.rzye,
        "rzye_first": first.rzye,
        "rz_change": rz_change,
        "rz_change_pct": rz_change_pct,
        "rz_net_total": rz_net,
        "rz_buy_total": rz_buy,
        "rqye_last": last.rqye,
        "rq_change": rq_change,
        "rq_sell_total": rq_sell,
        "rzrqye_last": last.rzrqye,
        "rzyezb_last": last.rzyezb,
        "sentiment": sentiment,
    }


# ------------------------------------------------------------------ 趋势与总状态

def trend_state(bars: Sequence[Bar], ma_summary: dict[str, Any]) -> dict[str, Any]:
    if len(bars) < 6:
        return {"short": "数据不足", "mid": "数据不足", "long": "数据不足", "label": "数据不足"}

    def chg(days: int) -> float | None:
        if len(bars) <= days:
            return None
        old = bars[-1 - days].close
        return round((bars[-1].close - old) / old * 100, 2) if old else None

    c5, c20, c60 = chg(5), chg(20), chg(60)

    def label(v: float | None, up: float, down: float) -> str:
        if v is None:
            return "数据不足"
        if v >= up:
            return "上涨"
        if v <= down:
            return "下跌"
        return "震荡"

    short = label(c5, 2.0, -2.0)
    mid = label(c20, 5.0, -5.0)
    long_ = label(c60, 8.0, -8.0)

    arrangement = ma_summary.get("arrangement", "")
    above_count = ma_summary.get("above_count", 0)
    if arrangement == "多头排列" and above_count >= 3:
        overall = "多头趋势"
    elif arrangement == "空头排列" and above_count <= 1:
        overall = "空头趋势"
    elif short == "上涨" and above_count >= 2:
        overall = "短期上涨"
    elif short == "下跌" and above_count <= 1:
        overall = "短期下跌"
    else:
        overall = "震荡整理"

    return {
        "short": short, "mid": mid, "long": long_, "label": overall,
        "chg_5d": c5, "chg_20d": c20, "chg_60d": c60,
    }


def build_status(
    quote: Quote,
    bars: Sequence[Bar],
    flow: dict[str, Any],
    margin: dict[str, Any],
    ma_summary: dict[str, Any],
    sr: dict[str, Any],
) -> dict[str, Any]:
    """需求 5.2.5：四类标准化状态标签。"""
    trend = trend_state(bars, ma_summary)
    tags = [
        {"group": "趋势状态", "label": trend["label"], "tone": _tone_trend(trend["label"])},
        {"group": "资金状态", "label": flow.get("state", "无数据"), "tone": _tone_flow(flow.get("state", ""))},
        {"group": "支撑压力", "label": sr.get("state", "数据不足"), "tone": _tone_sr(sr.get("state", ""))},
        {"group": "两融情绪", "label": margin.get("sentiment", "无两融数据"), "tone": _tone_margin(margin.get("sentiment", ""))},
        {"group": "均线形态", "label": ma_summary.get("arrangement", "均线交织"), "tone": _tone_trend(ma_summary.get("arrangement", ""))},
    ]
    if quote.status != "normal" and quote.status_text:
        tags.insert(0, {"group": "交易状态", "label": quote.status_text, "tone": "warn"})
    return {"trend": trend, "tags": tags}


def _tone_trend(label: str) -> str:
    if label in ("多头趋势", "短期上涨", "多头排列", "短期多头"):
        return "up"
    if label in ("空头趋势", "短期下跌", "空头排列", "短期空头"):
        return "down"
    return "flat"


def _tone_flow(label: str) -> str:
    return {"主力净流入": "up", "主力净流出": "down"}.get(label, "flat")


def _tone_sr(label: str) -> str:
    if "突破" in label or "上半区" in label:
        return "up"
    if "跌破" in label or "下半区" in label:
        return "down"
    return "flat"


def _tone_margin(label: str) -> str:
    if "做多" in label:
        return "up"
    if "做空" in label or "撤离" in label:
        return "down"
    return "flat"
