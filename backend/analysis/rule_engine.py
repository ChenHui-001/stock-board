"""无 LLM 时的确定性决策层。

`rule_based()` 是 `analysis.analyze()` 的回退路径：LLM 不可用 / 调用失败 /
被校验逻辑撤销时使用，输出与 LLM 路径**结构完全一致**。

模块边界：
  - 本模块只关心"打分 + 文字结论"，不关心如何喂给 LLM（看 prompts.py），
    也不关心 LLM 输出如何校验（看 sanitize.py）。
  - `prompts.py` 反向 import 本模块的 `_intraday_score / _annotate_intraday`
    用于在投喂 LLM 的 payload 中携带当日盘口信号。无循环依赖问题：
    本模块不引用 prompts / sanitize。
"""
from __future__ import annotations

import logging
from typing import Any

from ..scorecfg import get_weights
from ..utils import confidence, describe_exc, round2

log = logging.getLogger("analysis.rule_engine")

ACTIONS = ["积极持仓/加仓", "持有观望", "减仓规避", "清仓离场"]

# ------------------------------------------------------------------ 因子权重（回测标定）
#
# 依据：因子级归因回测（10 只 A 股 × 800 根日线，7050 事件，2023-09 ~ 2026-08，
# 脚本见 tmp/factor_attrib.py，报告 tmp/factor_attrib/因子归因报告.md）。
# 关键结论是各因子 IC 在 7 个半年期上的**方向稳定性**，而非样本内收益高低：
#
#   因子              权重    IC 为正期数   说明
#   站上均线条数       0.35    3/7          符号反复翻转，噪音
#   均线排列           0.35    1/7          稳定为负，追涨信号有害
#   四均线斜率         0.35    0/7          稳定为负
#   20 日涨跌          0.35    0/7          稳定为负，IC 最负（-0.095）
#   支撑压力突破       0.35    1/7          稳定为负
#   MA20 乖离修正      2.50    7/7          **唯一全期为正**，IR 1.54 最高
#   盘口信号           1.00    4/7          方向不稳，但样本外未显著为负，保留原权重
#
# 注意：这里做的是**降权而非反向**。回测中「动量取反」在全样本拿到 t=4.05，
# 但样本外验证 t=-2.25（失效甚至反向），因此不采纳反向，只把方向被证伪的
# 趋势跟随类因子压到不足以主导决策的量级。
FACTOR_WEIGHTS: dict[str, float] = {
    "above": 0.35,
    "arrange": 0.35,
    "slope": 0.35,
    "chg20": 0.35,
    "sr": 0.35,
    "dev": 2.5,
    "intraday": 1.0,
}

# 决策阈值（原 28 / 5 / -22 为经验值，从未回测）
# 重新标定：因子降权后技术面量级由 ±78 压缩到约 ±40，旧阈值下加仓档几乎消失
# （回测仅 0.2% 事件触发），必须同步重标。
# 标定方法见 tmp/calibrate_threshold.py：训练期 2023-09~2024-12 网格搜索，
# 约束四档分布合理（加仓 12~32%、清仓 18~40%），目标为加仓档胜率不低于清仓档。
#   - 训练期：加仓-清仓胜率差由 -3.6pct 改善到 +2.0pct
#   - 测试期（2025-01 起，未参与标定）：由 -1.7pct 改善到 +1.8pct，方向一致
# 附带的分布变化：观望档占比 16.7% → 41.1%，清仓档 34.0% → 17.6%，
# 在信号缺乏预测力的前提下，减少激进误动作本身即为收益。
TH_ADD = 12.0      # >= 加仓
TH_HOLD = -6.0     # >= 观望
TH_REDUCE = -16.0  # >= 减仓，否则清仓

# 置信度区间（原 45~92，基础值 68）：回测已证明评分方向性有限——总分 IC 在
# 7 个半年期里 0/7 为正，加仓档胜率 48.4% 反而低于清仓档 50.7%。
# 若保持旧公式，一个方向被证伪的评分仍会输出 90+ 的置信度，等于用高置信度
# 驱动加仓。故收敛到 35~78，并把基础值降到 50：中性评分应落在中性置信度上。
CONF_MIN, CONF_MAX, CONF_BASE = 35, 78, 50
CONF_PER_POINT = 0.25   # 每 1 分评分对应的置信度增量（原为 1/3 ≈ 0.333）


def _damp(signal_label: str) -> float:
    """按盘口信号的支撑样本量衰减权重：样本越少，越不该主导决策。

    SIGNAL_RELIABILITY 里各信号的 n 极度不均（交投清淡 n=249，放量下挫 n=1）。
    原先 n=1 的信号与 n=249 的信号等值参与打分，等于让个例驱动决策。
    """
    n = int(SIGNAL_RELIABILITY[signal_label].get("n") or 0)
    if n >= 50:
        return 1.0
    if n >= 10:
        return 0.8
    return 0.25


def _round_half_away(value: float) -> int:
    """四舍五入且远离零（避免 Python 银行家舍入把 -0.5 变成 0）。"""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


# ------------------------------------------------------------------ 辅助评分

def _confidence_reason_text(
    score: float,
    aligned: bool,
    conflict: bool,
    news_n: int,
    report_n: int,
    fin_n: int,
    fin_stale: bool,
) -> str:
    """根据信号一致性 + 样本量 + 数据新鲜度压缩成一句话置信度说明。"""
    parts: list[str] = []
    if conflict:
        parts.append("技术/资金/消息三面背离")
    elif aligned:
        parts.append("三面同向共振")
    else:
        parts.append("三面信号方向不一")
    sample_bits = []
    if news_n < 3:
        sample_bits.append("资讯偏少")
    if report_n < 2:
        sample_bits.append("研报偏少")
    if fin_n == 0:
        sample_bits.append("无财报参考")
    elif fin_stale:
        sample_bits.append("财报源不可用、参考上次缓存")
    if sample_bits:
        parts.append("、".join(sample_bits) + "拉低置信度")
    if abs(score) >= TH_ADD:
        parts.append("评分极端，进一步增强置信度")
    return "，".join(parts) if parts else "样本与信号均充足"


def _news_score(news: list[dict[str, Any]] | None) -> tuple[int, int, int]:
    """统计资讯情绪：返回 (利好数, 利空数, 中性数)。

    AI 解读的 sentiment 来自规则引擎或 LLM，二者情绪维度一致。
    """
    bull = bear = neutral = 0
    for n in news or []:
        sentiment = (n.get("interpretation") or {}).get("sentiment", "中性")
        if sentiment == "利好":
            bull += 1
        elif sentiment == "利空":
            bear += 1
        else:
            neutral += 1
    return bull, bear, neutral


def _reports_score(reports: list[dict[str, Any]] | None) -> tuple[int, int, int]:
    """统计券商研报情绪：返回 (利好数, 利空数, 中性数)。"""
    bull = bear = neutral = 0
    for r in reports or []:
        sentiment = (r.get("interpretation") or {}).get("sentiment", "中性")
        if sentiment == "利好":
            bull += 1
        elif sentiment == "利空":
            bear += 1
        else:
            neutral += 1
    return bull, bear, neutral


def _fundamental_score(financials: dict[str, Any] | None) -> tuple[int, list[str], dict[str, Any]]:
    """按最新定期报告同比与质量指标给出保守基本面分，不跨口径比较绝对值。"""
    rows = (financials or {}).get("rows") or []
    if not rows:
        return 0, [], {"summary": "暂无季报/中报/年报数据", "period": "--", "growth": "暂无同比数据", "quality": "暂无质量指标"}
    dated_rows = [row for row in rows if str(row.get("date") or "")]
    latest = max(dated_rows, key=lambda row: str(row.get("date"))) if dated_rows else rows[0]
    score = 0
    notes: list[str] = []
    growth_bits: list[str] = []
    for label, key in (("营收", "revenue_yoy"), ("归母净利润", "net_profit_yoy"), ("扣非净利润", "net_profit_deduct_yoy")):
        value = latest.get(key)
        if not isinstance(value, (int, float)):
            continue
        growth_bits.append(f"{label}同比 {value:+.2f}%")
        if value >= 5:
            score += 2
        elif value <= -5:
            score -= 2
    roe = latest.get("roe")
    debt = latest.get("debt_ratio")
    if isinstance(roe, (int, float)):
        if roe >= 10:
            score += 1
        elif roe < 0:
            score -= 1
    if isinstance(debt, (int, float)) and debt >= 70:
        score -= 1
    score = max(-8, min(8, score))
    period = latest.get("period") or latest.get("date") or "未知报告期"
    growth = "，".join(growth_bits) or "暂无同比数据"
    stale_note = "财报源暂不可用，以下为上次成功缓存数据；" if financials.get("stale") else ""
    quality_bits = []
    if isinstance(roe, (int, float)):
        quality_bits.append(f"ROE {roe:.2f}%")
    if isinstance(latest.get("gross_margin"), (int, float)):
        quality_bits.append(f"毛利率 {latest['gross_margin']:.2f}%")
    if isinstance(debt, (int, float)):
        quality_bits.append(f"负债率 {debt:.2f}%")
    quality = "，".join(quality_bits) or "暂无质量指标"
    if score > 0:
        notes.append(f"{stale_note}最新{period}基本面偏强：{growth}")
    elif score < 0:
        notes.append(f"{stale_note}最新{period}基本面承压：{growth}")
    return score, notes, {
        "summary": stale_note + ("基本面偏强" if score > 0 else "基本面承压" if score < 0 else "基本面分化"),
        "period": period,
        "growth": growth,
        "quality": quality,
    }


def _volume_confirm(bars: list[dict[str, Any]]) -> tuple[int, str]:
    """量能确认：近 5 日涨跌 × 最近一日量能 vs 前 5 日均量。

    返回 (加分, 说明)。放量上涨/缩量下跌偏多，放量下跌/缩量上涨偏空。
    volume 单位约定为股（各源已统一）。

    长期量能参照：地量股（近 5 日均量不足近 60 日均量 70%）的「放量」需更大幅度
    才有信号意义，阈值从 1.3 提升到 1.5，降低地量股小反弹被误判为放量的概率。
    """
    if len(bars) < 6:
        return 0, ""
    closes = [b.get("close") for b in bars[-6:]]
    vols = [b.get("volume") for b in bars[-6:]]
    if any(c is None for c in closes) or any(v is None or v <= 0 for v in vols):
        return 0, ""
    if not closes[-6]:
        return 0, ""
    chg5 = (closes[-1] - closes[-6]) / closes[-6] * 100
    base = sum(vols[:-1]) / 5
    recent = vols[-1] / base if base else 0.0

    volume_threshold = 1.3
    if len(bars) >= 60:
        avg60 = sum(b.get("volume") or 0 for b in bars[-60:]) / 60
        if avg60 > 0 and base < avg60 * 0.7:
            volume_threshold = 1.5

    if chg5 > 1 and recent >= volume_threshold:
        return 6, f"近5日涨 {chg5:.1f}% 且放量（量比 {recent:.1f}），量价配合良好"
    if chg5 < -1 and recent >= volume_threshold:
        return -6, f"近5日跌 {abs(chg5):.1f}% 且放量（量比 {recent:.1f}），抛压较重"
    if chg5 < -1 and recent <= 0.7:
        return 3, f"近5日跌 {abs(chg5):.1f}% 但缩量（量比 {recent:.1f}），抛压减轻"
    if chg5 > 1 and recent <= 0.7:
        return -3, f"近5日涨 {chg5:.1f}% 但缩量（量比 {recent:.1f}），涨势缺乏量能确认"
    return 0, ""


# ------------------------------------------------------------------ 盘口信号 + 可靠性

# 盘口信号历史可靠性（基于两次离线回测：backend/backtest 盘口信号策略 大样本日线回测
# ≈1596 样本，盘中 vs 收盘对照实验 14:00 真实快照 240 样本）：
#   strength: 高=两时点命中率≥52% 或大样本≥53%；中=样本不足但方向有逻辑支撑；
#             低=命中率显著低于基线（如振幅收敛，撤销看多方向）。
#   n:        支撑该判断的回测样本数（与 strength 同源，取自大样本/对照实验统计）；
#              经 utils.confidence 折算为置信度（≥100 高 / 50-99 中 / <50 低）。
#   hit:      盘中/收盘命中率展示文本。
SIGNAL_RELIABILITY: dict[str, dict[str, str]] = {
    "高位强势": {"strength": "中", "n": 128, "hit": "盘中54.7% / 收盘43.8%",
                "note": "盘中时点有效，收盘时点偏弱（冲高后有均值回归压力）"},
    "冲高回落": {"strength": "高", "n": 17, "hit": "盘中57.1% / 收盘52.9%",
                "note": "两个时点命中率均超52%，短线抛压信号可靠"},
    "低位回升": {"strength": "中", "n": 6, "hit": "样本不足",
                "note": "空头衰竭逻辑支撑，但回测样本少，仅供参考"},
    "低位下跌": {"strength": "高", "n": 75, "hit": "大样本53.8% / 盘中42.0%",
                "note": "大样本回测命中率53.8%高于基线，弱势信号可靠"},
    "放量上攻": {"strength": "中", "n": 4, "hit": "样本不足",
                "note": "量价配合逻辑明确，但触发样本少，仅供参考"},
    "放量下挫": {"strength": "中", "n": 1, "hit": "样本不足",
                "note": "抛压集中释放逻辑明确，但触发样本少，仅供参考"},
    "缩量上涨": {"strength": "高", "n": 13, "hit": "盘中63.2% / 收盘69.2%",
                "note": "两个时点命中率均超63%，涨势不实信号可靠"},
    "缩量下跌": {"strength": "高", "n": 17, "hit": "盘中64.3% / 收盘58.8%",
                "note": "两个时点命中率均超58%，抛压减轻信号可靠"},
    "振幅剧烈": {"strength": "中", "n": 1, "hit": "样本不足",
                "note": "波动风险逻辑明确，但触发样本少，仅供参考"},
    "振幅收敛": {"strength": "低", "n": 69, "hit": "盘中41.7% / 收盘53.6%→41.7%",
                "note": "盘中命中率低于基线，看多方向已被回测撤销"},
    "换手活跃": {"strength": "中", "n": 2, "hit": "未统计",
                "note": "交投活跃逻辑明确，暂无独立回测样本"},
    "换手出货": {"strength": "中", "n": 1, "hit": "未统计",
                "note": "高换手+下跌逻辑明确，暂无独立回测样本"},
    "交投清淡": {"strength": "高", "n": 249, "hit": "大样本54.3% / 盘中52.5%",
                "note": "大样本与盘中命中率均超52%，清淡信号可靠"},
}

# 信号关键词匹配（与 backend/backtest/intraday_strategy.SIGNAL_RULES 同口径，生产代码不依赖回测模块）
_SIGNAL_KEYS: list[tuple[str, tuple[str, ...]]] = [
    ("高位强势", ("当日高位", "强势")),
    ("冲高回落", ("高位", "回落")),
    ("低位回升", ("低位", "回升")),
    ("低位下跌", ("低位", "下跌")),
    ("放量上攻", ("放量上攻",)),
    ("放量下挫", ("放量下挫",)),
    ("缩量上涨", ("缩量上涨",)),
    ("缩量下跌", ("缩量下跌",)),
    ("振幅剧烈", ("振幅", "剧烈")),
    ("振幅收敛", ("振幅", "收敛")),
    ("换手活跃", ("交投活跃",)),
    ("换手出货", ("分歧出货",)),
    ("交投清淡", ("交投清淡",)),
]


def _intraday_score(q: dict[str, Any]) -> tuple[int, str]:
    """当日盘口分项：盘中位置×涨跌方向 + 量比 + 振幅 + 换手对技术面的修正。

    返回 (加分, 说明)。盘口数据缺失时返回 (0, "")，不影响其它分项。
    权重校准依据见 backend/backtest 盘口信号策略（日线近似）+ 盘中对照实验。
    """
    price, prev = q.get("price"), q.get("prev_close")
    hi, lo = q.get("high"), q.get("low")
    chg = q.get("change_pct")
    if not (price and prev and hi and lo and hi > lo and chg is not None):
        return 0, ""

    pos = (price - lo) / (hi - lo) * 100
    amp = (hi - lo) / prev * 100
    vr = q.get("volume_ratio")
    turnover = q.get("turnover")
    score = 0
    bits: list[str] = []

    # 盘中位置 × 涨跌方向：高位强势/冲高回落、低位弱势/空头衰竭
    # 各分支按自身支撑样本量衰减（见 _damp），样本不足的信号不再等值参与决策
    if pos >= 75:
        if chg > 0:
            score += 3 * _damp("高位强势")
            bits.append(f"现价运行至当日高位（{pos:.0f}%）且上涨，多头强势（注意冲高后回归压力）")
        else:
            score -= 4 * _damp("冲高回落")
            bits.append(f"现价自当日高位回落（{pos:.0f}%）转跌，短线抛压显现")
    elif pos <= 25:
        if chg < 0:
            score -= 4 * _damp("低位下跌")
            bits.append(f"现价贴近当日低位（{pos:.0f}%）且下跌，弱势明显")
        else:
            score += 2 * _damp("低位回升")
            bits.append(f"现价自当日低位（{pos:.0f}%）回升，空头动能衰竭")

    # 量比：放量验证方向 / 缩量削弱信号
    if vr is not None:
        if vr >= 2:
            if chg > 0:
                score += 3 * _damp("放量上攻")
                bits.append(f"量比 {vr:.2f} 放量上攻，量价配合良好")
            else:
                score -= 3 * _damp("放量下挫")
                bits.append(f"量比 {vr:.2f} 放量下挫，抛压集中释放")
        elif vr <= 0.6:
            if chg > 0:
                score -= 1 * _damp("缩量上涨")
                bits.append(f"量比 {vr:.2f} 缩量上涨，涨势动能存疑")
            else:
                score += 1 * _damp("缩量下跌")
                bits.append(f"量比 {vr:.2f} 缩量下跌，抛压有所减轻")

    # 振幅：剧烈波动是风险；收敛不进分（回测显示振幅收敛次日上涨率 42.2% 反向）
    if amp >= 8:
        score -= 2 * _damp("振幅剧烈")
        bits.append(f"当日振幅 {amp:.1f}%，波动剧烈")
    elif amp <= 1.5:
        bits.append(f"当日振幅 {amp:.1f}%，走势收敛")

    # 换手：极高警惕分歧出货；极低交投清淡
    if turnover is not None:
        if turnover >= 10:
            if chg < 0:
                score += -2 * _damp("换手出货")
            else:
                score += 1 * _damp("换手活跃")
            bits.append(f"换手率 {turnover:.1f}% 偏高，{'分歧出货风险' if chg < 0 else '交投活跃'}")
        elif turnover <= 0.8:
            score -= 3 * _damp("交投清淡")
            bits.append(f"换手率 {turnover:.1f}% 过低，交投清淡")

    return _round_half_away(max(-8.0, min(8.0, score))), "；".join(bits)


def _annotate_intraday(note: str) -> list[dict[str, Any]]:
    """把盘口说明（；分隔的多信号）拆成带历史可靠性标注的条目。

    返回 [{"text", "strength", "hit", "note", "confidence"}]；
    confidence 由支撑样本数经 utils.confidence 折算（与自检/回测口径统一），
    未匹配到已知信号的子句保留原文不标注（strength=""）。
    """
    out: list[dict[str, str]] = []
    for part in note.split("；"):
        part = part.strip()
        if not part:
            continue
        item: dict[str, str] = {"text": part, "strength": "", "hit": "", "note": "", "confidence": None}  # type: ignore[typeddict-item]
        for label, keys in _SIGNAL_KEYS:
            if all(k in part for k in keys):
                info = SIGNAL_RELIABILITY[label]
                item["strength"] = info["strength"]
                item["hit"] = info["hit"]
                item["note"] = f"{label}：{info['note']}"
                item["confidence"] = confidence(info["n"])
                break
        out.append(item)
    return out


# ------------------------------------------------------------------ rule_based 主入口

def rule_based(
    detail: dict[str, Any],
    news: list[dict[str, Any]] | None = None,
    reports: list[dict[str, Any]] | None = None,
    financials: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """无 LLM（或 LLM 失败）时的确定性降级分析，输出结构完全一致。"""
    q = detail["quote"]
    financials = financials or detail.get("financials") or {}
    ma = {m["window"]: m for m in detail.get("ma", [])}
    ma_sum = detail.get("ma_summary", {})
    flow = detail.get("fund_flow", {}).get("summary", {})
    margin = detail.get("margin", {}).get("summary", {})
    sr = detail.get("support_resistance", {})
    trend = detail.get("status", {}).get("trend", {})
    price = q.get("price") or 0.0

    # 当日实时盘口：振幅 / 盘中位置 / 今开跳空 / 量比 / 换手
    prev_close = q.get("prev_close")
    hi, lo, opn = q.get("high"), q.get("low"), q.get("open")
    amp = (hi - lo) / prev_close * 100 if (hi and lo and prev_close) else None
    intraday_pos = (price - lo) / (hi - lo) * 100 if (price and hi and lo and hi > lo) else None
    gap = (opn - prev_close) / prev_close * 100 if (opn and prev_close) else None
    vol_ratio = q.get("volume_ratio")
    turnover = q.get("turnover")
    change = q.get("change")
    change_pct = q.get("change_pct")

    def yi(v: Any) -> str:
        if v is None:
            return "无数据"
        v = float(v)
        if abs(v) >= 1e8:
            return f"{v / 1e8:.2f}亿元"
        return f"{v / 1e4:.0f}万元"

    def mval(w: int) -> str:
        info = ma.get(w) or {}
        return "--" if info.get("value") is None else f"{info['value']:.2f}"

    def mpos(w: int) -> str:
        return (ma.get(w) or {}).get("position", "数据不足")

    # ==================== 三维分面评分 ====================
    # 技术面：均线结构 / 排列 / 斜率 / 区间 / 乖离
    # 各因子按 FACTOR_WEIGHTS 加权（回测标定，见文件顶部注释）
    W = FACTOR_WEIGHTS
    tech_score = 0.0
    above = ma_sum.get("above_count", 0)
    tech_score += (above - 2) * 8 * W["above"]
    arrangement = ma_sum.get("arrangement", "")
    tech_score += {"多头排列": 18, "短期多头": 8, "空头排列": -18, "短期空头": -8}.get(arrangement, 0) * W["arrange"]
    for w, weight in ((5, 3), (10, 3), (20, 4), (60, 4)):
        slope = (ma.get(w) or {}).get("slope")
        tech_score += (weight if slope == "上行" else (-weight if slope == "下行" else 0)) * W["slope"]

    chg20 = trend.get("chg_20d")
    if chg20 is not None:
        tech_score += (6 if chg20 > 0 else -6) * W["chg20"]

    sr_state = sr.get("state", "")
    if "突破" in sr_state:
        tech_score += 8 * W["sr"]
    elif "跌破" in sr_state:
        tech_score -= 12 * W["sr"]

    # 乖离修正：现价偏离 MA20 过大时提示超买/超卖风险。
    # 这是全部因子中唯一方向在 7/7 个半年期保持为正的信号，故提权。
    ma20v = (ma.get(20) or {}).get("value")
    deviation_note = ""
    if price and ma20v:
        dev_pct = (price - ma20v) / ma20v * 100
        if dev_pct > 8:
            tech_score -= 4 * W["dev"]
            deviation_note = f"现价较 MA20 乖离 {dev_pct:.1f}%（超买）"
        elif dev_pct < -8:
            tech_score += 4 * W["dev"]
            deviation_note = f"现价较 MA20 乖离 {dev_pct:.1f}%（超卖）"

    # 摆动指标（MACD/KDJ）：仅分析展示与 LLM 投喂，不参与评分与结论
    osc = detail.get("oscillators") or {}
    macd = osc.get("macd") or {}
    kdj = osc.get("kdj") or {}
    osc_note: list[str] = []
    macd_cross = macd.get("cross", "")
    if macd_cross == "金叉":
        osc_note.append(f"MACD 金叉（DIF {macd.get('dif')} 上穿 DEA {macd.get('dea')}），趋势转多（仅参考）")
    elif macd_cross == "死叉":
        osc_note.append(f"MACD 死叉（DIF {macd.get('dif')} 下穿 DEA {macd.get('dea')}），趋势转弱（仅参考）")
    hist_trend = macd.get("hist_trend", "")
    if "放大" in hist_trend:
        osc_note.append(f"MACD {'红柱放大，多头动能增强' if '红' in hist_trend else '绿柱放大，空头动能增强'}")
    kdj_cross = kdj.get("cross", "")
    if kdj_cross == "金叉":
        osc_note.append(f"KDJ 金叉（K {kdj.get('k')} 上穿 D {kdj.get('d')}）（仅参考）")
    elif kdj_cross == "死叉":
        osc_note.append(f"KDJ 死叉（K {kdj.get('k')} 下穿 D {kdj.get('d')}）（仅参考）")
    kdj_zone = kdj.get("zone", "")
    if kdj_zone == "超买":
        osc_note.append(f"KDJ 超买（J {kdj.get('j')}），短线偏热（仅参考）")
    elif kdj_zone == "超卖":
        osc_note.append(f"KDJ 超卖（J {kdj.get('j')}），短线偏冷（仅参考）")
    osc_summary = "；".join(osc_note)

    # 当日盘口分项（技术面修正）
    intraday_pts, intraday_note = _intraday_score(q)
    tech_score += intraday_pts * W["intraday"]

    # 资金面：当日主力为主 / 近5日辅 / 连续流向 / 两融 / 量能确认
    # 当日资金流向未发布时主项改用近5日口径，避免把前日 main_last 当成「当日」数据参与评分。
    capital_score = 0
    main_last = flow.get("main_last") or 0
    main_last5 = flow.get("main_last5") or 0
    main_total = flow.get("main_total") or 0
    flow_fresh = bool(flow.get("fresh", False))
    flow_last_date = flow.get("last_date") or ""
    primary = main_last if flow_fresh else main_last5
    secondary = main_last5 if flow_fresh else main_total
    capital_score += 12 if primary > 0 else (-12 if primary < 0 else 0)
    capital_score += 6 if secondary > 0 else (-6 if secondary < 0 else 0)
    if flow.get("streak", 0) >= 3:
        capital_score += 6 if flow.get("streak_dir") == "流入" else -6

    rz_pct = margin.get("rz_change_pct")
    if rz_pct is not None:
        capital_score += 8 if rz_pct >= 5 else (-8 if rz_pct <= -5 else 0)

    # 量能确认
    volume_pts, volume_note = _volume_confirm(detail.get("kline", []))
    capital_score += volume_pts

    # 消息面：资讯 + 研报
    bull_n, bear_n, _ = _news_score(news)
    news_pts = max(-12, min(12, bull_n * 4 - bear_n * 4))
    bull_r, bear_r, _ = _reports_score(reports)
    report_pts = max(-15, min(15, bull_r * 5 - bear_r * 5))
    fundamental_pts, fundamental_notes, fundamental_text = _fundamental_score(financials)
    news_score = news_pts + report_pts

    # 三维权重（环境变量 / 设置页可配，默认 1.0）
    w = get_weights()
    w_tech = round(tech_score * w["tech"], 1)
    w_capital = round(capital_score * w["capital"], 1)
    w_news = round(news_score * w["news"], 1)
    score = round(w_tech + w_capital + w_news + fundamental_pts, 1)

    # ==================== 信号一致性 ====================
    def _dir(v: float) -> int:
        return 1 if v > 0 else (-1 if v < 0 else 0)

    dirs = [d for d in (_dir(tech_score), _dir(capital_score), _dir(news_score)) if d != 0]
    signal_conflict = len(set(dirs)) > 1
    signal_aligned = len(dirs) >= 2 and len(set(dirs)) == 1
    signal_note = ""
    if signal_conflict:
        signal_note = "技术面/资金面/消息面方向不一致，信号背离，建议观望确认后再操作"
    elif signal_aligned:
        signal_note = "技术面/资金面/消息面方向一致，信号共振增强"

    if score >= TH_ADD:
        action = ACTIONS[0]
        position = "可持有 7 成以上仓位，回踩不破 MA10 可加仓"
    elif score >= TH_HOLD:
        action = ACTIONS[1]
        position = "维持现有 5-7 成仓位，不加不减"
    elif score >= TH_REDUCE:
        action = ACTIONS[2]
        position = "仓位降至 3 成以下，反弹至压力位分批减"
    else:
        action = ACTIONS[3]
        position = "清空持仓，不留底仓"

    # 信号冲突时激进操作降一档
    if signal_conflict:
        if action == ACTIONS[0]:
            action = ACTIONS[1]
            position = "技术/资金/消息面背离，暂不加仓，维持 5-7 成等待方向确认"
        elif action == ACTIONS[3]:
            action = ACTIONS[2]
            position = "技术/资金/消息面背离，暂不清仓，先降至 3 成以下观察"

    support = sr.get("support") or (round2(price * 0.95) if price else None)
    resistance = sr.get("resistance") or (round2(price * 1.05) if price else None)
    stop_loss = round2(support * 0.97) if support else None
    take_profit = round2(resistance * 1.02) if resistance else None

    def zone(center: float | None, width: float = 0.015) -> str:
        if not center:
            return "--"
        return f"{center * (1 - width):.2f}-{center * (1 + width):.2f}"

    opportunities: list[str] = []
    risks: list[str] = []
    if above >= 3:
        opportunities.append(f"股价站上 {above} 条均线，MA20={mval(20)} 构成有效支撑")
    if arrangement in ("多头排列", "短期多头"):
        opportunities.append(f"均线{arrangement}，MA5={mval(5)} > MA10={mval(10)}，趋势结构完好")
    flow_day_label = "当日" if flow_fresh else f"最近交易日（{flow_last_date}）"
    if primary > 0:
        opportunities.append(
            f"{flow_day_label}主力净流入 {yi(primary)}（近5日 {yi(main_last5)} / 近30日累计 {yi(main_total)}），"
            f"其中超大单 {yi(flow.get('xl_total'))}"
        )
    if rz_pct is not None and rz_pct > 0:
        opportunities.append(f"融资余额30日增长 {rz_pct:.2f}%，杠杆资金做多意愿抬升")
    if "突破" in sr_state:
        opportunities.append(f"{sr_state}，上方压力位 {resistance} 已被消化")
    if bull_n >= 2:
        opportunities.append(f"近30日资讯面偏暖：{bull_n} 条利好（含 {bear_n} 条利空），情绪支撑明显")
    elif bull_n == 1 and bear_n == 0:
        opportunities.append("近30日资讯面有 1 条利好信号，暂无利空扰动")
    if bull_r >= 2:
        opportunities.append(f"券商研报面偏暖：{bull_r} 条买入/增持评级（含 {bear_r} 条谨慎），机构认可度较高")
    elif bull_r == 1 and bear_r == 0:
        opportunities.append("券商研报面有 1 条买入/增持评级信号，机构关注度提升")
    if fundamental_pts > 0:
        opportunities.extend(fundamental_notes)
    if volume_pts > 0 and volume_note:
        opportunities.append(volume_note)
    elif volume_pts < 0 and volume_note:
        risks.append(volume_note)
    if deviation_note:
        if "超卖" in deviation_note:
            opportunities.append(deviation_note + "，超跌反弹空间或已打开")
        else:
            risks.append(deviation_note + "，追高风险较大")
    if above <= 1:
        risks.append(f"股价仅站上 {above} 条均线，MA20={mval(20)} 压制明显（{mpos(20)}）")
    if arrangement in ("空头排列", "短期空头"):
        risks.append(f"均线{arrangement}，短期反弹易在 MA20={mval(20)} 受阻")
    if primary < 0:
        risks.append(
            f"{flow_day_label}主力净流出 {yi(abs(primary))}（近5日 {yi(main_last5)} / 近30日累计 {yi(main_total)}），"
            f"超大单 {yi(flow.get('xl_total'))}"
        )
    if (flow.get("sm_total") or 0) > 0 and main_last < 0 and flow.get("tiered"):
        risks.append("主力流出而小单流入，散户接盘特征明显")
    if rz_pct is not None and rz_pct < -3:
        risks.append(f"融资余额30日下降 {abs(rz_pct):.2f}%，杠杆资金撤离")
    if "跌破" in sr_state:
        risks.append(f"{sr_state}，下方支撑位 {support} 若失守将打开下跌空间")
    if q.get("status") != "normal" and q.get("status_text"):
        risks.append(q["status_text"])
    if bear_n >= 2:
        risks.append(f"近30日资讯面偏冷：{bear_n} 条利空（含 {bull_n} 条利好），情绪面承压")
    elif bear_n == 1 and bull_n == 0:
        risks.append("近30日资讯面有 1 条利空信号，需留意发酵扩散")
    if bear_r >= 2:
        risks.append(f"券商研报面偏冷：{bear_r} 条减持/谨慎评级，机构分歧加大，留意预期下修")
    elif bear_r == 1 and bull_r == 0:
        risks.append("券商研报面有 1 条减持/谨慎评级，需关注机构预期变化")
    if fundamental_pts < 0:
        risks.extend(fundamental_notes)

    if not opportunities:
        opportunities.append(f"当前价 {price:.2f} 距 20 日低点 {sr.get('low_20')} 有安全边际，超跌反弹可期")
    if not risks:
        risks.append(f"上方 {resistance} 为 {sr.get('resistance_from') or '近期高点'}，突破前存在震荡消化需求")

    # 当日盘中实时描述
    intraday_text = (
        f"现价 {price:.2f}"
        + (f"（{change:+.2f} / {change_pct:+.2f}%）" if change is not None and change_pct is not None else "")
        + (f"，今开 {opn:.2f}（" + ("高开" if (gap or 0) > 0 else "低开" if (gap or 0) < 0 else "平开")
           + f"{abs(gap or 0):.2f}%）" if opn else "")
        + (f"，最高 {hi:.2f} / 最低 {lo:.2f}" if (hi and lo) else "")
        + (f"，当日振幅 {amp:.2f}%" if amp is not None else "")
        + (f"，现价处于当日区间 {intraday_pos:.0f}% 位置" if intraday_pos is not None else "")
        + (f"，量比 {vol_ratio:.2f}" if vol_ratio is not None else "")
        + (f"，换手率 {turnover:.2f}%" if turnover is not None else "")
    )
    intraday_activity = (
        f"当日成交额 {yi(q.get('amount'))}"
        + (f"，量比 {vol_ratio:.2f}" if vol_ratio is not None else "")
        + (f"，换手率 {turnover:.2f}%" if turnover is not None else "")
        + ("，放量活跃" if (vol_ratio or 0) >= 2 else "，交投清淡" if (vol_ratio or 0) <= 0.6 else "")
    )

    # 当日盘口提示：按信号拆分并标注历史命中率强度
    if intraday_pts != 0 and intraday_note:
        target = opportunities if intraday_pts > 0 else risks
        target.extend(_annotate_intraday(intraday_note))

    return {
        "trend": {
            "summary": f"{trend.get('label', '震荡整理')}，{arrangement or '均线交织'}",
            "intraday": intraday_text,
            "short": (
                f"近5日涨跌 {trend.get('chg_5d')}%，MA5={mval(5)}（{(ma.get(5) or {}).get('slope', '--')}）、"
                f"MA10={mval(10)}，股价{mpos(5)} MA5、{mpos(10)} MA10，短期判定为{trend.get('short', '震荡')}。"
            ),
            "mid": (
                f"近20日涨跌 {trend.get('chg_20d')}%，MA20={mval(20)}（{(ma.get(20) or {}).get('slope', '--')}），"
                f"股价{mpos(20)} MA20，当前{sr.get('state', '')}，"
                f"20日区间 {sr.get('low_20')}-{sr.get('high_20')}，中期判定为{trend.get('mid', '震荡')}。"
            ),
            "long": (
                f"近60日涨跌 {trend.get('chg_60d')}%，MA60（季线）={mval(60)}（{(ma.get(60) or {}).get('slope', '--')}），"
                f"股价{mpos(60)} 季线，60日区间 {sr.get('low_60')}-{sr.get('high_60')}，"
                f"中长期判定为{trend.get('long', '震荡')}。"
            ),
            "pattern": f"{arrangement or '均线交织'}，{sr.get('state', '')}",
            "oscillators": (
                f"MACD：DIF {macd.get('dif')} / DEA {macd.get('dea')} / 柱 {macd.get('hist')}"
                f"（{macd_cross or '无交叉'}，{hist_trend or '柱平'}）；"
                f"KDJ：K {kdj.get('k')} / D {kdj.get('d')} / J {kdj.get('j')}"
                f"（{kdj_cross or '无交叉'}，{kdj_zone or '中性'}）"
                if (macd or kdj) and osc_summary else "MACD/KDJ 数据不足（K线少于 35 根）"
            ),
        },
        "capital": {
            "summary": f"{flow.get('state', '资金观望')}，{margin.get('sentiment', '两融情绪平稳')}",
            "intraday": intraday_activity,
            "main_force": (
                f"当日主力 {yi(main_last)}，近5日 {yi(main_last5)}，"
                f"近30日合计 {yi(main_total)}（流入 {flow.get('inflow_days', 0)} 天 / "
                f"流出 {flow.get('outflow_days', 0)} 天），"
                f"当前连续{flow.get('streak_dir', '持平')} {flow.get('streak', 0)} 天，"
                + (f"超大单 {yi(flow.get('xl_total'))}、大单 {yi(flow.get('lg_total'))}，"
                   if flow.get("tiered") else f"超大单 {yi(flow.get('xl_total'))}，")
                + f"整体呈{flow.get('trend', '震荡反复')}。"
            ),
            "retail": (
                f"中单 {yi(flow.get('md_total'))}、小单（散户）{yi(flow.get('sm_total'))}，"
                + ("散户在主力流出时接盘，筹码由强手转弱手。"
                   if (flow.get("sm_total") or 0) > 0 and main_last < 0
                   else "散户资金与主力方向一致，分歧不大。")
                if flow.get("tiered")
                else "当前资金数据来自备用源，仅提供净流入与超大单口径，无大单/中单/小单四档拆分，散户维度不参与本次判定。"
            ),
            "margin": (
                f"最新融资余额 {yi(margin.get('rzye_last'))}，30日变动 {rz_pct}%，"
                f"融资净买入合计 {yi(margin.get('rz_net_total'))}；"
                f"融券余额 {yi(margin.get('rqye_last'))}，判定为{margin.get('sentiment', '两融情绪平稳')}。"
                if margin.get("available") else "该股无两融标的数据，杠杆资金维度不参与本次判定。"
            ),
        },
        "fundamental": fundamental_text,
        "risk": {"opportunities": opportunities[:4], "risks": risks[:4]},
        "advice": {
            "action": action,
            "reason": (
                f"综合评分 {score}：均线站上 {above}/4 条且{arrangement or '交织'}，"
                f"30日主力{yi(main_total)}，{margin.get('sentiment', '两融平稳')}，{sr_state}"
                + (f"，盘口 {intraday_pts:+d} 分" if intraday_pts else "")
                + (f"，资讯面 {bull_n} 利好/{bear_n} 利空（计 {news_pts:+d} 分）" if (news or []) else "")
                + (f"，研报面 {bull_r} 利好/{bear_r} 利空（计 {report_pts:+d} 分）" if (reports or []) else "")
                + (f"，财报面 {fundamental_text['period']} {fundamental_pts:+d} 分" if (financials or {}).get("rows") else "")
                + (f"，{signal_note}" if signal_note else "")
                + "。"
            ),
            "confidence": max(
                CONF_MIN,
                min(CONF_MAX, CONF_BASE + int(abs(score) * CONF_PER_POINT)
                    + (8 if signal_aligned else (-12 if signal_conflict else 0))),
            ),
            "position": position,
            "support": support,
            "resistance": resistance,
            "entry_zone": zone(support),
            "exit_zone": zone(resistance),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "horizon": "5-10 个交易日",
            "score": score,
            "confidence_reason": _confidence_reason_text(
                score, signal_aligned, signal_conflict,
                len(news or []), len(reports or []),
                len((financials or {}).get("rows") or []),
                bool(financials.get("stale")),
            ),
            "scores": {
                "tech": w_tech,
                "capital": w_capital,
                "news": w_news,
                "fundamental": fundamental_pts,
                "intraday": intraday_pts,
                "total": score,
            },
            "weights": w,
            "signal": "conflict" if signal_conflict else ("aligned" if signal_aligned else "neutral"),
            "signal_note": signal_note,
        },
    }
