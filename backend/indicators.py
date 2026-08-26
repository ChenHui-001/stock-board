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

# ------------------------------------------------------------------ ATR（平均真实波幅）

# ATR 周期：14 日是 A 股/海外日线默认，与 KDJ(9)/MACD(12,26,9) 同量级行业标准
ATR_PERIOD = 14


def compute_atr(bars: Sequence[Bar], period: int = ATR_PERIOD) -> list[float | None]:
    """计算每根 Bar 的 ATR（Wilder 平滑）。

    真实波幅 TR_i = max(high-low, |high-prev_close|, |low-prev_close|)，
    首根用 high-low；之后用 Wilder 递推：ATR_i = (ATR_{i-1} * (period-1) + TR_i) / period。

    数据不足 period+1 根时（首根无法与 prev_close 比较）返回 None 列表，
    让上层按"无 ATR"路径走固定阈值，保持向后兼容。
    返回长度与 bars 一致；前 period 个值为 None，从第 period 根起开始有 ATR。
    """
    n = len(bars)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out

    # 1) 计算 TR 序列
    trs: list[float] = [0.0] * n
    trs[0] = bars[0].high - bars[0].low
    for i in range(1, n):
        hi, lo = bars[i].high, bars[i].low
        prev_close = bars[i - 1].close
        trs[i] = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))

    # 2) 初始 ATR = 前 period 根 TR 的简单平均（含首根 tr0）
    init = sum(trs[:period]) / period
    out[period - 1] = round(init, 4)
    prev = init
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = round(prev, 4)
    return out


def decorate_bars_with_atr(bars: list[Bar], period: int = ATR_PERIOD) -> float | None:
    """把 ATR(period) 写入每根 Bar.atr（原地），并返回最后一根的 ATR 值。
    数据不足时所有 Bar.atr 保持 None，返回 None 让上层走兜底逻辑。"""
    atr_seq = compute_atr(bars, period=period)
    last = None
    for bar, v in zip(bars, atr_seq):
        bar.atr = v
        if v is not None:
            last = v
    return last


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


def support_resistance(
    bars: Sequence[Bar],
    price: float | None,
    ma_values: dict[int, float | None],
    atr: float | None = None,
) -> dict[str, Any]:
    """用近 20/60 日高低点 + 均线，取最近的下方支撑与上方压力。

    突破/跌破容差同时考虑「价格固定比例」与「0.5 倍 ATR」：
      tolerance = max(price * 0.5%, 0.5 * atr)
    这样高波动股票不会被噪声触发突破，低波动股票也不会因容差太大永远到不了。
    ATR 不可用时退回到原 price*0.5% 行为，保持向后兼容。
    """
    result: dict[str, Any] = {
        "support": None, "resistance": None,
        "support_from": "", "resistance_from": "",
        "high_20": None, "low_20": None, "high_60": None, "low_60": None,
        "state": "数据不足",
        "atr": round(atr, 4) if atr else None,
        "atr_breakout": "未触及",
        "secondary_support": [], "secondary_resistance": [],
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

    # 容差：price*0.5% 与 0.5*ATR 取较大者；ATR 不可用时退化为 price*0.5%
    tol = max(price * 0.005, 0.5 * atr) if atr else price * 0.005

    below = [(v, tag) for v, tag in candidates if v <= price - tol]
    above = [(v, tag) for v, tag in candidates if v >= price + tol]
    if below:
        v, tag = max(below, key=lambda x: x[0])
        result["support"], result["support_from"] = round(v, 2), tag
    if above:
        v, tag = min(above, key=lambda x: x[0])
        result["resistance"], result["resistance_from"] = round(v, 2), tag

    # P1-4：列出次要支撑/压力候选（最多 3 个），给用户看"下一个位置"
    # 排除最近的那一对（已经在 support/resistance），按距离现价的远近排序
    primary_support = result.get("support")
    primary_resistance = result.get("resistance")
    secondary_support: list[dict[str, Any]] = []
    secondary_resistance: list[dict[str, Any]] = []
    for v, tag in below:
        v_r = round(v, 2)
        if v_r != primary_support:
            secondary_support.append({"price": v_r, "from": tag})
    for v, tag in above:
        v_r = round(v, 2)
        if v_r != primary_resistance:
            secondary_resistance.append({"price": v_r, "from": tag})
    secondary_support.sort(key=lambda x: x["price"], reverse=True)
    secondary_resistance.sort(key=lambda x: x["price"])
    result["secondary_support"] = secondary_support[:3]
    result["secondary_resistance"] = secondary_resistance[:3]

    # 区间位置判定：用同样的 ATR 容差决定「已突破」与「逼近」
    if price >= high20 + tol:
        result["state"] = "突破区间上沿"
        result["atr_breakout"] = "已突破"
    elif price >= high20 - tol:
        result["state"] = "逼近压力位"
        result["atr_breakout"] = "逼近"
    elif price <= low20 - tol:
        result["state"] = "跌破区间下沿"
        result["atr_breakout"] = "已跌破"
    elif price <= low20 + tol:
        result["state"] = "逼近支撑位"
        result["atr_breakout"] = "逼近"
    else:
        span = high20 - low20
        pos = (price - low20) / span if span else 0.5
        # P2-8：区间位置阈值用 ATR 归一化
        # 半区阈值 = max(33%, 4 个 ATR / 区间宽度)，让高波动股票不会永远被判上半区
        if atr and span > 0 and price > 0:
            atr_band = (4 * atr) / span  # 4 个 ATR 在区间内占多大比例
            upper_threshold = max(0.66, atr_band)
            lower_threshold = min(0.34, 1 - atr_band)
        else:
            upper_threshold = 0.66
            lower_threshold = 0.34
        # 用 1e-9 容差避免浮点边界（pos=0.8000000000000007 vs upper=0.8）
        if pos > upper_threshold + 1e-9:
            result["state"] = "运行于区间上半区"
        elif pos < lower_threshold - 1e-9:
            result["state"] = "运行于区间下半区"
        else:
            result["state"] = "区间中枢震荡"
        result["atr_breakout"] = "未触及"
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
            return ("主力抢筹（当日）" if strong_in else "主力净流入（当日）"), "inflow"
        if main_last < 0:
            return ("主力出逃（当日）" if strong_out else "主力净流出（当日）"), "outflow"
        return "资金观望（当日）", "neutral"

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

    # P1-6：streak_text 让前端副标题展示「连 3 日流入」等依据，
    # 用户看到 5 档分级的连续性基础（不只是看一个标签）
    streak_text = ""
    if streak >= 2 and streak_dir in ("流入", "流出"):
        streak_text = f"连 {streak} 日{streak_dir}"

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
        "streak_text": streak_text,
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

    # 两融数据 T+1 公布：用户看到的是"最新披露日"数据，追加日期后缀避免被误读为实时
    last_date_short = (last.date or "")[:10]  # 形如 2026-08-25
    sentiment_with_date = (
        f"{sentiment}（截至 {last_date_short}）" if last_date_short else sentiment
    )

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
        "sentiment_with_date": sentiment_with_date,
        "last_date": last_date_short,
    }


# ------------------------------------------------------------------ 趋势与总状态

def trend_state(
    bars: Sequence[Bar],
    ma_summary: dict[str, Any],
    atr: float | None = None,
) -> dict[str, Any]:
    """趋势状态：基于 N 日涨跌幅 + ATR 归一化判定方向。

    ATR 归一化思路：把固定百分比阈值改成"相当于多少倍 ATR"，
    利用时间平方根法则把 5/20/60 日换算到统一基准：
        unit_atr = cN / (atr/price * sqrt(N))
    阈值表：
        |unit_atr| < 1.0 → 震荡
        unit_atr ≥ 1.0  → 上涨
        unit_atr ≤ -1.0 → 下跌
    ATR 不可用时退回到原 ±2%/±5%/±8% 阈值。
    """
    if len(bars) < 6:
        return {"short": "数据不足", "mid": "数据不足", "long": "数据不足", "label": "数据不足"}

    def chg(days: int) -> float | None:
        if len(bars) <= days:
            return None
        old = bars[-1 - days].close
        return round((bars[-1].close - old) / old * 100, 2) if old else None

    c5, c20, c60 = chg(5), chg(20), chg(60)

    # ATR 归一化：unit_atr = (cN_pct/100) / (atr/price * sqrt(N))
    # 返回值便于 AI/前端引用「近5日波幅相当于多少个 ATR」
    vol_unit_atr: dict[str, float | None] = {"chg_5d": None, "chg_20d": None, "chg_60d": None}
    if atr and bars[-1].close:
        ref = atr / bars[-1].close
        for key, c, days in (("chg_5d", c5, 5), ("chg_20d", c20, 20), ("chg_60d", c60, 60)):
            if c is not None and ref > 0:
                vol_unit_atr[key] = round((c / 100) / (ref * (days ** 0.5)), 2)

    def label(v: float | None, up: float, down: float) -> str:
        if v is None:
            return "数据不足"
        if v >= up:
            return "上涨"
        if v <= down:
            return "下跌"
        return "震荡"

    if atr and bars[-1].close:
        # ATR 模式：以 ±1.0 个 ATR 为震荡/趋势分界
        def label_atr(u: float | None) -> str:
            if u is None:
                return "数据不足"
            if u >= 1.0:
                return "上涨"
            if u <= -1.0:
                return "下跌"
            return "震荡"
        short = label_atr(vol_unit_atr["chg_5d"])
        mid = label_atr(vol_unit_atr["chg_20d"])
        long_ = label_atr(vol_unit_atr["chg_60d"])
    else:
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

    # P1-5：量能验证——近 1 日成交量 / 近 5 日均量
    # 量比 < 0.7：缩量（趋势可信度低）；> 1.3：放量（趋势可信度高）
    vol_5d_ratio: float | None = None
    if len(bars) >= 6:
        last_vol = bars[-1].volume or 0
        avg_5d_vol = sum(b.volume or 0 for b in bars[-6:-1]) / 5
        if avg_5d_vol > 0:
            vol_5d_ratio = round(last_vol / avg_5d_vol, 2)
    if vol_5d_ratio is None:
        volume_confirm = ""
    elif vol_5d_ratio >= 1.3:
        volume_confirm = "放量"
    elif vol_5d_ratio <= 0.7:
        volume_confirm = "缩量"
    else:
        volume_confirm = "平量"

    return {
        "short": short, "mid": mid, "long": long_, "label": overall,
        "chg_5d": c5, "chg_20d": c20, "chg_60d": c60,
        "atr": round(atr, 4) if atr else None,
        "vol_unit_atr": vol_unit_atr,
        "atr_normalized": bool(atr and bars[-1].close),
        "vol_5d_ratio": vol_5d_ratio,
        "volume_confirm": volume_confirm,
    }


def intraday_trend_state(bars_min: Sequence[Bar]) -> dict[str, Any]:
    """P2-7：盘中实时趋势（60 分钟 K 线）。

    与日线 trend_state 区分：本函数只看当日开市以来的分钟线，给用户一个"当下"趋势判断，
    而不是昨日收盘的趋势快照。返回：
      - available: bool  是否有有效分钟数据
      - label:    "上涨" / "下跌" / "震荡"
      - chg:      当日累计涨跌 %
      - bars:     有效分钟 K 线根数

    判定逻辑：
      - 至少 6 根有效 60 分钟 K 线（覆盖 6 小时，约 2.5 个交易日，足够判方向）
      - 用首尾 close 比较：
          |chg| >= 1.0% → 上涨/下跌
          否则          → 震荡
      - 数据不足时返回 available=False，让上层不展示子标签（避免误导）
    """
    out: dict[str, Any] = {"available": False, "label": "数据不足",
                            "chg": None, "bars": 0}
    if not bars_min:
        return out
    valid = [b for b in bars_min if b.close]
    if len(valid) < 6:
        out["bars"] = len(valid)
        return out
    first_close = valid[0].close
    last_close = valid[-1].close
    if not first_close:
        out["bars"] = len(valid)
        return out
    chg = round((last_close - first_close) / first_close * 100, 2)
    if chg >= 1.0:
        label = "上涨"
    elif chg <= -1.0:
        label = "下跌"
    else:
        label = "震荡"
    out.update(available=True, label=label, chg=chg, bars=len(valid))
    return out


def build_status(
    quote: Quote,
    bars: Sequence[Bar],
    flow: dict[str, Any],
    margin: dict[str, Any],
    ma_summary: dict[str, Any],
    sr: dict[str, Any],
    atr: float | None = None,
    pre_open: bool = False,
    delayed: bool = False,
    intraday: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """需求 5.2.5：四类标准化状态标签。

    pre_open / delayed 两个开关用于统一控制标签输出：
      - pre_open=True（盘前 9:30 之前）：所有数据类标签降级为"待开盘"，
        避免用户看到"昨日数据当实时"的误导。
      - delayed=True（数据源延迟/长假未更新）：所有数据类标签降级为"数据延迟"，
        并用 warn 色提醒，避免静默误导。
    intraday（P2-7）传入盘中 60 分钟 K 线判定的实时趋势，available 时附加子标签。
    两个开关同时为 True 时 pre_open 优先（盘前本身就是"没数据"状态）。
    """
    trend = trend_state(bars, ma_summary, atr=atr)
    # 震荡细分：基于5日涨跌 + 资金状态 grade 区分方向偏好
    refined_trend_label = _refine_choppy_label(
        trend["label"], trend.get("chg_5d"), flow.get("state_grade"))
    # 盘中一致性：当日 60 分线趋势 vs 昨日日线趋势
    consistency = intraday_consistency(refined_trend_label, intraday)

    # 趋势状态主标签：盘中一致时附加当日累计%；背离时降 warn 色
    trend_label = _trend_label_with_volume({"label": refined_trend_label,
                                              "volume_confirm": trend.get("volume_confirm")})
    trend_tone = _tone_trend(refined_trend_label)
    if consistency["aligned"] is True and consistency["chg"] is not None:
        chg_text = f"{consistency['chg']:+.2f}%"
        trend_label = f"{trend_label} · 盘中{chg_text}"
    elif consistency["aligned"] is False:
        trend_tone = "warn"  # 盘中日线背离时染色警示

    # 盘口信号：仅在盘中有数据时（非 pre_open + 非 delayed）才评估
    intraday_signal = {"label": "", "tone": "flat"}
    if not pre_open and not delayed:
        intraday_signal = intraday_state_from_quote(quote)

    if pre_open:
        # 盘前：所有数据类标签统一为"待开盘"，连带均线形态也置灰
        placeholders = {
            "趋势状态": "待开盘",
            "资金状态": "待开盘",
            "支撑压力": "待开盘",
            "两融情绪": "待开盘",
            "均线形态": "待开盘",
            "盘口位置": "待开盘",
        }
        tags = [
            {"group": g, "label": placeholders[g], "tone": "warn"}
            for g in ("趋势状态", "资金状态", "支撑压力", "两融情绪", "均线形态", "盘口位置")
        ]
    elif delayed:
        # 数据延迟：标签保留原内容便于诊断，但 tone 全部置 warn 染色警示
        # 原写法 `_tone_flow(...) or "warn"` 只在 tone 为空时才回退为 warn，
        # 导致资金状态仍按"up/down"染色，与延迟语义矛盾 → 强制全 warn
        tags = [
            {"group": "趋势状态", "label": trend_label, "tone": "warn"},
            {"group": "资金状态", "label": flow.get("state", "无数据"), "tone": "warn"},
            {"group": "支撑压力", "label": sr.get("state", "数据不足"), "tone": "warn"},
            {"group": "两融情绪", "label": margin.get("sentiment_with_date", margin.get("sentiment", "无两融数据")), "tone": "warn"},
            {"group": "均线形态", "label": ma_summary.get("arrangement", "均线交织"), "tone": "warn"},
            {"group": "盘口位置", "label": intraday_signal["label"] or "数据延迟", "tone": "warn"},
        ]
    else:
        # 正常时段：两融情绪 label 加披露日期后缀，避免 T+1 延迟数据被误读为实时
        tags = [
            {"group": "趋势状态", "label": trend_label, "tone": trend_tone},
            {"group": "资金状态", "label": flow.get("state", "无数据"), "tone": _tone_flow(flow.get("state", ""))},
            {"group": "支撑压力", "label": sr.get("state", "数据不足"), "tone": _tone_sr(sr.get("state", ""))},
            {"group": "两融情绪", "label": margin.get("sentiment_with_date", margin.get("sentiment", "无两融数据")), "tone": _tone_margin(margin.get("sentiment", ""))},
            {"group": "均线形态", "label": ma_summary.get("arrangement", "均线交织"), "tone": _tone_trend(ma_summary.get("arrangement", ""))},
            {"group": "盘口位置", "label": intraday_signal["label"] or "—", "tone": intraday_signal["tone"]},
        ]

    # P2-7：盘中 60 分钟趋势作为子标签附加
    # 仅当 available=True 时展示，避免给用户"今日已涨 0.3%"这种噪声
    if intraday and intraday.get("available"):
        chg_text = (f"{intraday['chg']:+.2f}%" if intraday.get("chg") is not None else "")
        if intraday["label"] == "上涨":
            intra_tone = "up"
        elif intraday["label"] == "下跌":
            intra_tone = "down"
        else:
            intra_tone = "flat"
        # label 形如 "60分线 上涨 +1.23%"（无涨跌时不带 %）
        label_text = "60分线 " + intraday["label"]
        if chg_text:
            label_text += " " + chg_text
        tags.append({"group": "盘中实时", "label": label_text, "tone": intra_tone})

    if quote.status != "normal" and quote.status_text:
        tags.insert(0, {"group": "交易状态", "label": quote.status_text, "tone": "warn"})
    # 盘中背离提示：仅在盘中趋势与日线背离时（aligned=False）返回 hint 文本，
    # 前端 status section 顶部据此显示「⚠ 盘中日线背离」警告条。
    # 抑制条件：
    #   - 一致时（aligned=True）：不返回，标签里已有「·盘中+X.XX%」，无需重复
    #   - 盘前/延迟时：consistency 仍可能判定背离，但此时页面顶部已有「数据延迟」
    #     黄色提示，再叠背离警告会重复噪音 → 强制置空
    if pre_open or delayed:
        divergence_hint = ""
    elif consistency.get("aligned") is False:
        divergence_hint = consistency.get("hint", "")
    else:
        divergence_hint = ""
    return {"trend": trend, "tags": tags, "divergence_hint": divergence_hint}


def _tone_trend(label: str) -> str:
    if label in ("多头趋势", "短期上涨", "多头排列", "短期多头"):
        return "up"
    if label in ("空头趋势", "短期下跌", "空头排列", "短期空头"):
        return "down"
    return "flat"


# ---------------------------------------------------------- 盘口信号（build_status 专用）
def intraday_state_from_quote(quote: Quote) -> dict[str, str]:
    """从 quote 提一个当日盘口主导信号：盘中位置×涨跌方向 + 量比×涨跌方向 + 振幅。

    返回 {label, tone}：与 build_status 同结构。
    量化阈值与 _intraday_score（analysis.py）一致但取最显著的一条作为标签，
    避免盘口条件多时标签爆炸。盘中数据缺失时返回空字符串 + flat（不展示）。
    """
    price = getattr(quote, "price", None)
    prev = getattr(quote, "prev_close", None)
    hi = getattr(quote, "high", None)
    lo = getattr(quote, "low", None)
    chg = getattr(quote, "change_pct", None)
    vr = getattr(quote, "volume_ratio", None)
    if not (price and prev and hi and lo and hi > lo and chg is not None):
        return {"label": "", "tone": "flat"}

    pos = (price - lo) / (hi - lo) * 100          # 0-100
    amp = (hi - lo) / prev * 100                  # %
    candidates: list[tuple[int, str, str]] = []  # (|score|, label, tone)

    # 盘中位置 × 涨跌方向
    if pos >= 75:
        if chg > 0:
            candidates.append((3, f"高位强势（{pos:.0f}%）", "up"))
        else:
            candidates.append((4, f"冲高回落（{pos:.0f}%）", "down"))
    elif pos <= 25:
        if chg < 0:
            candidates.append((4, f"弱势探底（{pos:.0f}%）", "down"))
        else:
            candidates.append((2, f"空头衰竭（{pos:.0f}%）", "up"))

    # 量比 × 涨跌方向
    if vr is not None:
        if vr >= 2:
            if chg > 0:
                candidates.append((3, f"放量上攻（量比 {vr:.2f}）", "up"))
            else:
                candidates.append((3, f"放量下挫（量比 {vr:.2f}）", "down"))
        elif vr <= 0.6:
            if chg > 0:
                candidates.append((1, f"缩量上涨（量比 {vr:.2f}）", "flat"))
            else:
                candidates.append((1, f"缩量下跌（量比 {vr:.2f}）", "flat"))

    # 振幅剧烈：单独一条
    if amp >= 8:
        candidates.append((2, f"波动剧烈（振幅 {amp:.1f}%）", "warn"))

    if not candidates:
        return {"label": "", "tone": "flat"}
    # 取 |score| 最大的那条：score 为负的看空信号与正的多头信号按绝对值排，
    # 否则 -4 的「冲高回落」会被 +2 的「波动剧烈」挤掉
    candidates.sort(key=lambda x: abs(x[0]), reverse=True)
    return {"label": candidates[0][1], "tone": candidates[0][2]}


# ---------------------------------------------------------- 震荡细分
def _refine_choppy_label(trend_label: str, chg_5d: float | None,
                          flow_grade: str | None) -> str:
    """把「震荡整理」细分为「偏多震荡/偏空震荡/中性震荡」。

    判定逻辑：5 日方向 × 资金方向，权重各半。
      短期正 + 资金流入或中性 → 偏多震荡
      短期负 + 资金流出或中性 → 偏空震荡
      其他 → 中性震荡
    非震荡 label 原样返回，保持 5 档趋势体系不变。
    """
    if trend_label != "震荡整理":
        return trend_label
    if chg_5d is None:
        return "中性震荡"
    grade = flow_grade or "neutral"
    if chg_5d > 0.5 and grade in ("inflow", "neutral"):
        return "偏多震荡"
    if chg_5d < -0.5 and grade in ("outflow", "neutral"):
        return "偏空震荡"
    return "中性震荡"


# ---------------------------------------------------------- 盘中趋势一致性
def intraday_consistency(trend_label: str, intraday: dict[str, Any] | None) -> dict[str, Any]:
    """盘中 60 分线趋势 vs 昨日日线趋势的一致性。

    返回 {aligned: bool, hint: str}：
      - aligned=True  → 主标签更新为 "{trend_label} · {当日累计%}"，反映当日实时
      - aligned=False → 主标签 tone 降 warn，hint 给背离说明
    intraday 不可用时返回 aligned=None（不参与调整），保持现状。
    """
    if not intraday or not intraday.get("available"):
        return {"aligned": None, "hint": "", "chg": None}
    intra_label = intraday.get("label", "")
    intra_chg = intraday.get("chg")
    # 一致性映射：昨日"上涨/多头" vs 盘中"上涨"；昨日"下跌/空头" vs 盘中"下跌"
    up_keys = {"上涨"}
    down_keys = {"下跌"}
    flat_keys = {"震荡"}
    if trend_label in ("多头趋势", "短期上涨", "多头排列", "短期多头") and intra_label in up_keys:
        return {"aligned": True, "hint": "盘中延续涨势", "chg": intra_chg}
    if trend_label in ("空头趋势", "短期下跌", "空头排列", "短期空头") and intra_label in down_keys:
        return {"aligned": True, "hint": "盘中延续跌势", "chg": intra_chg}
    if trend_label in ("多头趋势", "短期上涨", "多头排列", "短期多头") and intra_label in down_keys:
        return {"aligned": False, "hint": "昨日多头盘中转弱", "chg": intra_chg}
    if trend_label in ("空头趋势", "短期下跌", "空头排列", "短期空头") and intra_label in up_keys:
        return {"aligned": False, "hint": "昨日空头盘中反弹", "chg": intra_chg}
    if trend_label == "震荡整理" and intra_label in flat_keys:
        return {"aligned": True, "hint": "盘中维持震荡", "chg": intra_chg}
    # 其他组合：涨跌 vs 震荡、震荡 vs 涨跌 等"中性分歧"
    return {"aligned": False, "hint": "盘中日线趋势分歧", "chg": intra_chg}


def _trend_label_with_volume(trend: dict[str, Any]) -> str:
    """P1-5：把量能验证结果拼到趋势 label 后面，让用户看到「短期上涨（缩量）」等可信度提示。

    震荡/数据不足时不加后缀，避免噪音。
    """
    label = trend.get("label", "")
    if not label or label in ("震荡整理", "数据不足"):
        return label
    vc = trend.get("volume_confirm", "")
    if vc:
        return f"{label}（{vc}）"
    return label


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
