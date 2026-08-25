"""技术指标与状态标签引擎。

产出的结构同时服务于：详情页「均线模块 / 状态模块」、AI 分析的输入投喂、
以及无 LLM 时的规则降级分析。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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

# ------------------------------------------------------------------ 摆动指标（MACD / KDJ）

def _ema(values: Sequence[float], span: int) -> list[float | None]:
    """指数移动平均。首个有效值取首元素，之后递推。"""
    if not values:
        return []
    k = 2 / (span + 1)
    out: list[float | None] = [None] * len(values)
    prev: float | None = None
    for i, v in enumerate(values):
        if prev is None:
            prev = float(v)
        else:
            prev = v * k + prev * (1 - k)
        out[i] = round(prev, 4)
    return out


def macd_series(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list[float | None]]:
    """MACD 序列：DIF / DEA / 柱（柱=(DIF-DEA)*2）。"""
    if len(closes) < slow + signal:
        return {"dif": [], "dea": [], "hist": []}
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [round(f - s, 4) if (f is not None and s is not None) else None
           for f, s in zip(ema_fast, ema_slow)]
    valid_dif = [d for d in dif if d is not None]
    dea = [None] * (len(dif) - len(valid_dif)) + _ema(valid_dif, signal)
    hist = [round((d - e) * 2, 4) if (d is not None and e is not None) else None
            for d, e in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "hist": hist}


def kdj_series(bars: Sequence[Bar], n: int = 9) -> dict[str, list[float | None]]:
    """KDJ 序列（9,3,3）：K / D / J。"""
    if len(bars) < n + 1:
        return {"k": [], "d": [], "j": []}
    k: list[float | None] = [None] * len(bars)
    d: list[float | None] = [None] * len(bars)
    j: list[float | None] = [None] * len(bars)
    pk = pd = 50.0
    for i in range(len(bars)):
        lo = min(b.low for b in bars[max(0, i - n + 1):i + 1])
        hi = max(b.high for b in bars[max(0, i - n + 1):i + 1])
        rsv = (bars[i].close - lo) / (hi - lo) * 100 if hi > lo else 50.0
        ck = round(pk * 2 / 3 + rsv / 3, 3)
        cd = round(pd * 2 / 3 + ck / 3, 3)
        k[i] = ck
        d[i] = cd
        j[i] = round(3 * ck - 2 * cd, 3)
        pk, pd = ck, cd
    return {"k": k, "d": d, "j": j}


def compute_oscillators(bars: Sequence[Bar]) -> dict[str, Any]:
    """MACD / KDJ 汇总：数值序列（近 30 根）+ 最新状态与信号。

    返回 {"macd": {dif, dea, hist, cross, hist_trend}, "kdj": {k, d, j, cross,
    zone}, "bars": n}；数据不足时返回空状态（不报错）。
    """
    closes = [b.close for b in bars]
    out: dict[str, Any] = {"bars": len(bars), "macd": {}, "kdj": {}}
    if len(bars) < 35:
        return out

    m = macd_series(closes)
    kd = kdj_series(bars)
    if not m["dif"] or not kd["k"]:
        return out

    # ---- MACD 状态
    dif, dea, hist = m["dif"], m["dea"], m["hist"]
    def _last(seq: Sequence[float | None]) -> float | None:
        return next((v for v in reversed(seq) if v is not None), None)
    def _prev(seq: Sequence[float | None]) -> float | None:
        valid = [v for v in seq if v is not None]
        return valid[-2] if len(valid) >= 2 else None

    cdif, cdea, chist = _last(dif), _last(dea), _last(hist)
    pdif, pdea, phist = _prev(dif), _prev(dea), _prev(hist)
    cross = ""
    if cdif is not None and cdea is not None and pdif is not None and pdea is not None:
        if pdif <= pdea and cdif > cdea:
            cross = "金叉"
        elif pdif >= pdea and cdif < cdea:
            cross = "死叉"
    hist_trend = ""
    if chist is not None and phist is not None:
        hist_trend = "红柱放大" if chist > 0 and chist > phist else (
            "红柱缩短" if chist > 0 else (
            "绿柱放大" if chist < phist else "绿柱缩短"))
    out["macd"] = {
        "dif": round(cdif, 3) if cdif is not None else None,
        "dea": round(cdea, 3) if cdea is not None else None,
        "hist": round(chist, 3) if chist is not None else None,
        "cross": cross,
        "hist_trend": hist_trend,
        "series": {
            "dif": [round(v, 3) if v is not None else None for v in dif[-30:]],
            "dea": [round(v, 3) if v is not None else None for v in dea[-30:]],
            "hist": [round(v, 3) if v is not None else None for v in hist[-30:]],
        },
    }

    # ---- KDJ 状态
    kk, dd, jj = kd["k"], kd["d"], kd["j"]
    ck, cdd, cj = _last(kk), _last(dd), _last(jj)
    pk2, pdd = _prev(kk), _prev(dd)
    kdj_cross = ""
    if ck is not None and cdd is not None and pk2 is not None and pdd is not None:
        if pk2 <= pdd and ck > cdd:
            kdj_cross = "金叉"
        elif pk2 >= pdd and ck < cdd:
            kdj_cross = "死叉"
    zone = ""
    if cj is not None:
        zone = "超买" if cj > 100 else ("超卖" if cj < 0 else ("偏强" if cj > 80 else ("偏弱" if cj < 20 else "中性")))
    out["kdj"] = {
        "k": round(ck, 2) if ck is not None else None,
        "d": round(cdd, 2) if cdd is not None else None,
        "j": round(cj, 2) if cj is not None else None,
        "cross": kdj_cross,
        "zone": zone,
        "series": {
            "k": [round(v, 2) if v is not None else None for v in kk[-30:]],
            "d": [round(v, 2) if v is not None else None for v in dd[-30:]],
            "j": [round(v, 2) if v is not None else None for v in jj[-30:]],
        },
    }
    return out


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


# ------------------------------------------------------------------ 资金价量背离

def _compute_price_flow_note(rows: Sequence[FlowDay]) -> str:
    """价量背离判定：用最近 5 日 vs 前 5 日的均价 / 资金均量方向对比，
    输出四象限信号。这是专业操盘最看重的指标——比单独看资金流入更能
    判断"主力在高位派发"还是"低位吸筹"。
    依赖 FlowDay.close（东财/新浪/akshare 都带）。
    要求至少 10 日数据：5 日「近期」+ 5 日「前期」，确保两边样本对称可比。"""
    if len(rows) < 10:
        return ""

    closes = [r.close for r in rows if r.close is not None]
    mains = [r.main for r in rows]
    if len(closes) < 10 or len(mains) < 10:
        return ""

    # 前期 = 前 5 日（indices 0-4），近期 = 后 5 日（indices -5 至末尾）
    price_prev = sum(closes[:5]) / 5
    price_now = sum(closes[-5:]) / 5
    flow_prev = sum(mains[:5]) / 5
    flow_now = sum(mains[-5:]) / 5

    # 用相对变化率判定方向，阈值避免噪声误判（价格 ±0.3%, 资金 ±5%）
    price_up = price_now > price_prev * 1.003
    price_down = price_now < price_prev * 0.997
    flow_up = flow_now > flow_prev * 1.05
    flow_down = flow_now < flow_prev * 0.95

    if price_up and flow_up:
        return "价格↑资金↑ 共振看多"
    if price_down and flow_down:
        return "价格↓资金↓ 共振看空"
    if price_up and flow_down:
        return "价格↑资金↓ 高位诱多"
    if price_down and flow_up:
        return "价格↓资金↑ 低位吸筹"
    return ""


def _compute_main_dominance(rows: Sequence[FlowDay]) -> str:
    """主力类型分类：超大单占比 = |xl| / (|main|+1)。
    占比越高说明越倾向机构资金主导，而非分散大户。
    新浪等兜底源没有 xl 拆分时返回空。"""
    if not rows:
        return ""
    xl_abs = sum(abs(r.xl) for r in rows)
    main_abs = sum(abs(r.main) for r in rows)
    if main_abs < 1e6:  # 样本金额太小（<100 万）跳过
        return ""
    if not any(r.xl for r in rows):  # 新浪兜底源 xl=0，无超大单数据
        return ""
    ratio = xl_abs / (main_abs + 1e-6)
    if ratio >= 0.7:
        return "机构主导（超大单占主力70%+）"
    if ratio >= 0.4:
        return "机构+大单混合（超大单占主力40-70%）"
    return "主力分散（超大单占主力40%以下）"


def _grade_flow_state(main_last: float, streak: int, streak_dir: str,
                      xl_dominance: str, fresh: bool, last5: float,
                      main_total: float) -> tuple[str, str]:
    """5 档资金状态分级：
        主力抢筹（连入3日+且超大单主导）
        主力净流入（普通净入）
        资金观望（接近0）
        主力净流出（普通净出）
        主力出逃（连出3日+且超大单主导）
    fresh=False 时降级为「近5日」口径，保留括号日期语义。"""
    # 是否超大单主导：xl_dominance 字符串前缀匹配
    is_inst = "机构主导" in xl_dominance
    strong_in = streak >= 3 and streak_dir == "流入" and is_inst
    strong_out = streak >= 3 and streak_dir == "流出" and is_inst

    suffix = "" if fresh else "（近5日）"

    if fresh:
        if main_last > 0:
            return ("主力抢筹" if strong_in else "主力净流入"), "inflow"
        if main_last < 0:
            return ("主力出逃" if strong_out else "主力净流出"), "outflow"
        return "资金观望", "neutral"

    # fresh=False：退回近5日/累计口径
    if last5 > 0 and main_total > 0:
        return ("主力抢筹" + suffix if strong_in else "主力净流入" + suffix), "inflow"
    if last5 < 0 and main_total < 0:
        return ("主力出逃" + suffix if strong_out else "主力净流出" + suffix), "outflow"
    return "资金观望", "neutral"


# ------------------------------------------------------------------ 资金流向

def summarize_flow(rows: Sequence[FlowDay], ref_date: str | None = None) -> dict[str, Any]:
    """资金流向汇总。ref_date 传已知最近交易日（如 K 线最新日期）用于判断
    「当日资金流向是否已发布」——东财/新浪的日级资金流向通常在收盘后
    16 点前后才更新当日数据，盘中及 16 点前最后一行是前一交易日。
    此时把 rows[-1] 当「当日」会误导（把昨天数据标成今天），
    因此降级为近5日/累计口径并在 fresh=False 中标注。
    """
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

    main_last = rows[-1].main
    last_date = (rows[-1].date or "")[:10]
    last5 = sum(r.main for r in rows[-5:])
    ratio = inflow_days / len(rows)

    # 当日资金流向可用性：最后一行日期是否已到参照的最近交易日（K线最新日期）。
    # K线盘中即含当日，资金流向 16 点后才出当日——盘中/16点前 fresh=False。
    fresh = False
    if ref_date:
        try:
            last_d = datetime.strptime(last_date, "%Y-%m-%d").date()
            ref_d = datetime.strptime(str(ref_date)[:10], "%Y-%m-%d").date()
            fresh = last_d >= ref_d
        except ValueError:
            fresh = False

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

    # 价量背离 + 主力类型（专业操盘维度，比单一资金流向更有信号价值）
    price_flow_note = _compute_price_flow_note(rows)
    xl_dominance = _compute_main_dominance(rows)
    streak_dir = "流入" if direction > 0 else ("流出" if direction < 0 else "持平")

    # 5 档状态分级：基于「金额方向 + 连续性 + 主力类型」三维评分。
    # fresh=False 时降级为「主力净流入（近5日）」语义，避免把昨日数据当「当日」。
    state, state_grade = _grade_flow_state(
        main_last=main_last, streak=streak, streak_dir=streak_dir,
        xl_dominance=xl_dominance, fresh=fresh,
        last5=last5, main_total=main_total,
    )

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
        "state_grade": state_grade,
        "price_flow_note": price_flow_note,
        "xl_dominance": xl_dominance,
        "last_date": last_date,
        "fresh": fresh,
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
    # 5 档分级：抢筹/流入=绿，出逃/流出=红，观望=中性。
    # 兼容旧版"主力净流入（近5日）"等带括号日期的标签。
    if "抢筹" in label or "流入" in label:
        return "up"
    if "出逃" in label or "流出" in label:
        return "down"
    return "flat"


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
