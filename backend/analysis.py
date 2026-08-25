"""AI 分析：全维度数据投喂 + 结构化输出，附无 LLM 时的规则引擎降级。

输出严格对齐需求 6.3：行情趋势分析 / 资金与两融情绪分析 / 风险与机会拆解 /
明确持仓操作建议（四选一，禁止模糊话术），并补充支撑压力与介入离场区间。
"""
from __future__ import annotations

import logging
from typing import Any

from . import llm
from .scorecfg import get_weights
from .utils import now, round2

log = logging.getLogger("analysis")

ACTIONS = ["积极持仓/加仓", "持有观望", "减仓规避", "清仓离场"]

SYSTEM_PROMPT = """你是一名 A 股量化分析师，擅长用均线技术形态、资金流向、两融数据与市场资讯做短中期持仓决策。

你会收到某只股票的全维度实时数据（JSON），其中包含「市场资讯」段（近一个月相关新闻与 AI 情绪标注）、
「券商观点」段（近一个月券商研报与 AI 情绪标注）和「财报数据_季报中报」段（季报、中报、三季报、年报核心指标及同比变化）。
请综合基本面与技术面判断：资讯、研报和财报可作为辅助依据，但必须基于数据本身做判断，禁止编造数据中不存在的消息面或财务数据；
若资讯/研报情绪明显（如连续利好/利空、机构集中买入评级），应在风险机会与操作建议中反映其权重，并在 reason 中提及。
若「财报数据_季报中报」标记为缓存或源不可用，必须明确说明数据可能延迟，不得将其表述为刚发布的最新财报。
财报分析必须标注最新报告期（Q1/H1/Q3/FY），同比数据优先于跨季度绝对值；单季度、半年报和年报不可混为同一口径，缺失指标不得臆测。营收、归母净利润、扣非净利润、ROE、负债率等指标如出现持续恶化，应在风险中体现；若持续增长，应在机会中体现。
技术面判断必须结合当日实时盘口（量比、换手率、盘中位置、振幅）验证短线动能，并引用其数值。
「技术指标_MACD_KDJ」段中的 MACD/KDJ 数值与状态（金叉/死叉/超买超卖）仅作参考展示，
不作为决策依据（当前市场行情下摆动指标频繁钝化失效）；请勿仅凭 MACD/KDJ 金叉死叉给出操作建议。
「盘口信号可靠性」段标注了每个当日触发信号的 历史强度/命中率/置信度：历史强度与命中率反映该信号历史有效性，
置信度反映支撑样本量（样本越深越可靠）。解读时按置信度权衡——高置信信号可作重要依据，
低置信（样本不足）信号仅作参考提示，不应单独支撑激进操作；请在风险机会描述中自然体现这一权衡。

【硬性要求】
1. 只输出一个 JSON 对象，不要输出任何解释文字或 Markdown 代码块。
2. 禁止使用「观望为主」「谨慎操作」「注意风险」「仅供参考」这类模糊、无法执行的话术。
3. action 字段必须严格取以下四个字符串之一，不得改写：
   "积极持仓/加仓"、"持有观望"、"减仓规避"、"清仓离场"
   注意「持有观望」指维持现有仓位不动，是明确的不操作指令，不等于模糊表态。
4. 所有价位必须是具体数字，精确到 2 位小数，且落在合理价格区间内。
5. 每段分析都要引用具体数值（均线值、乖离率、资金金额、融资余额变化等）作为依据。

【输出 JSON 结构】
{
  "trend": {
    "summary": "一句话结论，20 字内",
    "short": "短期（5-10日）走势分析，引用 MA5/MA10 与近 5 日涨跌幅",
    "mid": "中期（20日）走势分析，引用 MA20 与区间位置",
    "long": "中长期（60日）走势分析，引用 MA60 与季线支撑压力",
    "pattern": "当前技术形态判定，如：多头排列后回踩 MA10 / 跌破 MA20 后反抽"
  },
  "capital": {
    "summary": "一句话结论，20 字内",
    "main_force": "主力资金动向分析，引用 30 日主力净额与连续流入/流出天数",
    "retail": "散户（中小单）情绪分析，引用小单净额",
    "margin": "两融多空分析，引用融资余额变化率与融券变化"
  },
  "fundamental": {
    "summary": "一句话结论，20 字内",
    "period": "引用最新财报期别，如 2026H1",
    "growth": "引用营收/归母净利润/扣非净利润同比变化",
    "quality": "引用 ROE、毛利率或负债率"
  },
  "risk": {
    "opportunities": ["利好机会 1", "利好机会 2"],
    "risks": ["风险点 1", "风险点 2"]
  },
  "advice": {
    "action": "四选一",
    "reason": "给出该结论的核心依据，60 字内，必须引用具体数据",
    "confidence": 75,
    "position": "建议仓位，如：可持有 5-7 成 / 降至 3 成以下 / 清空",
    "support": 12.34,
    "resistance": 15.67,
    "entry_zone": "12.80-13.20",
    "exit_zone": "15.40-15.80",
    "stop_loss": 12.10,
    "take_profit": 15.90,
    "horizon": "建议持有周期，如：5-10 个交易日"
  }
}"""


# ------------------------------------------------------------------ 投喂数据

def build_payload(
    detail: dict[str, Any],
    news: list[dict[str, Any]] | None = None,
    reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把详情页数据压缩成适合投喂 LLM 的紧凑结构（需求 6.2 全维度）。

    news 为近一个月相关资讯（含逐条情绪解读），reports 为券商研报（含逐条
    情绪解读），分别作为资讯面 / 券商观点面权重投喂。
    """
    q = detail["quote"]
    flow_rows = detail.get("fund_flow", {}).get("rows", [])
    margin_rows = detail.get("margin", {}).get("rows", [])
    bars = detail.get("kline", [])

    def yi(v: Any) -> float | None:
        """元 -> 亿元，便于模型理解量级。"""
        return None if v is None else round(float(v) / 1e8, 4)

    # 当日实时盘口：振幅 / 盘中位置 / 今开跳空
    price, prev = q.get("price"), q.get("prev_close")
    hi, lo, opn = q.get("high"), q.get("low"), q.get("open")
    amp = round((hi - lo) / prev * 100, 2) if (hi and lo and prev) else None
    intraday_pos = round((price - lo) / (hi - lo) * 100, 1) if (price and hi and lo and hi > lo) else None
    gap = round((opn - prev) / prev * 100, 2) if (opn and prev) else None

    # 近 60 日均量（万手）：作为「当日/近5日是否放量」的长期参照基准，
    # 避免模型仅凭近 5 日均量误判异动（如长期地量股短期小幅放量被误读为「放量上攻」）。
    # volume 单位为股，1 万手 = 1e6 股，与「成交量_万手」口径一致。
    bars_60 = bars[-60:]
    avg_vol_60 = (
        round(sum(b.get("volume") or 0 for b in bars_60) / max(len(bars_60), 1) / 1e6, 1)
        if bars_60 else None
    )

    return {
        "基础数据": {
            "股票名称": q.get("name"),
            "股票代码": q.get("code"),
            "所属板块": detail.get("boards") or ([q.get("board")] if q.get("board") else []),
            "现价": price,
            "昨收": prev,
            "涨跌额": q.get("change"),
            "涨跌幅%": q.get("change_pct"),
            "今开": opn,
            "最高": hi,
            "最低": lo,
            "今开跳空%": gap,
            "当日振幅%": amp,
            "盘中位置%": intraday_pos,  # 现价处于当日高低区间的相对位置 0-100
            "量比": q.get("volume_ratio"),
            "成交量_万手": round((q.get("volume") or 0) / 1e6, 2),
            "近60日均量_万手": avg_vol_60,  # 放量/缩量的长期参照基准
            "成交额_亿元": yi(q.get("amount")),
            "换手率%": q.get("turnover"),
            "交易状态": q.get("status_text") or "正常交易",
        },
        "均线技术数据": {
            "均线": [
                {
                    "均线": f"MA{m['window']}",
                    "数值": m["value"],
                    "自身走向": m["slope"],
                    "近5日变动%": m["slope_pct"],
                    "股价位置": m["position"],
                    "乖离率%": m["deviation_pct"],
                }
                for m in detail.get("ma", [])
            ],
            "排列形态": detail.get("ma_summary", {}).get("arrangement"),
            "站上均线": detail.get("ma_summary", {}).get("above"),
            "跌破均线": detail.get("ma_summary", {}).get("below"),
            "近5日涨跌幅%": detail.get("status", {}).get("trend", {}).get("chg_5d"),
            "近20日涨跌幅%": detail.get("status", {}).get("trend", {}).get("chg_20d"),
            "近60日涨跌幅%": detail.get("status", {}).get("trend", {}).get("chg_60d"),
            "近30日收盘序列": [round(b["close"], 2) for b in bars[-30:]],
            # 60 日收盘序列供模型观察中长期趋势形态（MA60 计算与区间位置需要足够样本）
            "近60日收盘序列": [round(b["close"], 2) for b in bars_60],
        },
        "技术指标_MACD_KDJ": _payload_oscillators(detail.get("oscillators") or {}),
        "支撑压力": detail.get("support_resistance", {}),
        "资金数据_近30日": {
            **{k: v for k, v in detail.get("fund_flow", {}).get("summary", {}).items()
               if k in ("trend", "state", "inflow_days", "outflow_days", "streak", "streak_dir")},
            "数据口径": (
                "完整四档（超大单/大单/中单/小单）"
                if detail.get("fund_flow", {}).get("summary", {}).get("tiered")
                else "备用源，仅有净流入与超大单两档，请勿臆测大单/中单/小单数据"
            ),
            "主力净额合计_亿元": yi(detail.get("fund_flow", {}).get("summary", {}).get("main_total")),
            "超大单合计_亿元": yi(detail.get("fund_flow", {}).get("summary", {}).get("xl_total")),
            "大单合计_亿元": yi(detail.get("fund_flow", {}).get("summary", {}).get("lg_total")),
            "中单合计_亿元": yi(detail.get("fund_flow", {}).get("summary", {}).get("md_total")),
            "小单合计_亿元": yi(detail.get("fund_flow", {}).get("summary", {}).get("sm_total")),
            "近5日主力净额_亿元": yi(detail.get("fund_flow", {}).get("summary", {}).get("main_last5")),
            "最新交易日主力净额_亿元": yi(detail.get("fund_flow", {}).get("summary", {}).get("main_last")),
            "资金流向最新日期": detail.get("fund_flow", {}).get("summary", {}).get("last_date"),
            "当日资金流向已发布": detail.get("fund_flow", {}).get("summary", {}).get("fresh", False),
            "逐日主力净额_万元": [
                [r["date"], round(r["main"] / 1e4, 1)] for r in flow_rows[-30:]
            ],
        },
        "两融数据_近30日": {
            "情绪": detail.get("margin", {}).get("summary", {}).get("sentiment"),
            "最新融资余额_亿元": yi(detail.get("margin", {}).get("summary", {}).get("rzye_last")),
            "融资余额30日变动%": detail.get("margin", {}).get("summary", {}).get("rz_change_pct"),
            "融资净买入合计_亿元": yi(detail.get("margin", {}).get("summary", {}).get("rz_net_total")),
            "最新融券余额_万元": (
                None if detail.get("margin", {}).get("summary", {}).get("rqye_last") is None
                else round(detail["margin"]["summary"]["rqye_last"] / 1e4, 1)
            ),
            "融券余额30日变动_万元": (
                None if detail.get("margin", {}).get("summary", {}).get("rq_change") is None
                else round(detail["margin"]["summary"]["rq_change"] / 1e4, 1)
            ),
            "融资余额占流通市值%": detail.get("margin", {}).get("summary", {}).get("rzyezb_last"),
            "有效交易日数": len(margin_rows),
        },
        "财报数据_季报中报": _payload_financials(detail.get("financials") or {}),
        "当前状态标签": [
            f"{t['group']}：{t['label']}" for t in detail.get("status", {}).get("tags", [])
        ],
        "盘口信号可靠性_当日": _payload_intraday_signals(q),
        "市场资讯_近30日": [
            {
                "日期": n.get("date", "")[:10],
                "来源": n.get("source", ""),
                "标题": n.get("title", ""),
                "摘要": (n.get("summary") or "")[:120],
                "AI情绪": (n.get("interpretation") or {}).get("sentiment", "中性"),
                "影响程度": (n.get("interpretation") or {}).get("impact", "低"),
                "解读": (n.get("interpretation") or {}).get("summary", ""),
            }
            for n in (news or [])[:15]
        ],
        "券商观点_近30日": [
            {
                "日期": r.get("date", "")[:10],
                "机构": r.get("source", ""),
                "研究员": r.get("researcher", ""),
                "评级": r.get("rating", ""),
                "标题": r.get("title", ""),
                "AI情绪": (r.get("interpretation") or {}).get("sentiment", "中性"),
                "影响程度": (r.get("interpretation") or {}).get("impact", "低"),
                "解读": (r.get("interpretation") or {}).get("summary", ""),
            }
            for r in (reports or [])[:10]
        ],
    }


# ------------------------------------------------------------------ 规则引擎

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
    """统计券商研报情绪：返回 (利好数, 利空数, 中性数)。

    与资讯一致，直接复用每条研报的 interpretation.sentiment（规则/LLM 双路径同维度）。
    """
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

    返回 (加分, 说明)。放量上涨/缩量下跌偏多，放量下跌/缩量上涨偏空，
    数据不足时返回 (0, "")。volume 单位约定为股（各源已统一）。

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

    # 放量阈值：地量股上调到 1.5，普通股保持 1.3
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


# 盘口信号历史可靠性（基于两次离线回测：backtest_intraday.py 大样本日线回测
# ≈1596 样本，backtest_compare.py 盘中 vs 收盘对照实验 14:00 真实快照 240 样本）：
#   strength: 高=两时点命中率≥52% 或大样本≥53%；中=样本不足但方向有逻辑支撑；
#             低=命中率显著低于基线（如振幅收敛，撤销看多方向）。
#   n:        支撑该判断的回测样本数（与 strength 同源，取自大样本/对照实验统计）；
#              经 utils.confidence 折算为置信度（≥100 高 / 50-99 中 / <50 低），
#              与自检面板/离线回测口径统一。
#   hit:      盘中/收盘命中率展示文本。
# 命中率受市场环境影响，此处用于让用户了解信号历史可靠性，不构成投资建议。
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

# 信号关键词匹配（与 backtest_intraday.SIGNAL_RULES 同口径，生产代码不依赖脚本）
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


def _annotate_intraday(note: str) -> list[dict[str, Any]]:
    """把盘口说明（；分隔的多信号）拆成带历史可靠性标注的条目。

    返回 [{"text", "strength", "hit", "note", "confidence"}]；
    confidence 由支撑样本数经 utils.confidence 折算（与自检/回测口径统一），
    未匹配到已知信号的子句保留原文不标注（strength=""）。
    """
    from .utils import confidence as _conf

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
                item["confidence"] = _conf(info["n"])
                break
        out.append(item)
    return out


def _payload_financials(financials: dict[str, Any]) -> dict[str, Any]:
    """压缩季报/中报/年报核心指标，保留报告期和同比口径供模型判断。"""
    rows = financials.get("rows") or []
    if not rows:
        return {"说明": "暂无可用季报/中报/年报数据"}

    def yi(v: Any) -> float | None:
        return None if v is None else round(float(v) / 1e8, 4)

    return {
        "数据源": financials.get("source", ""),
        "数据状态": "源暂不可用，使用上次成功缓存" if financials.get("stale") else "当前成功数据",
        "数据错误": financials.get("error", "") if financials.get("stale") else "",
        "最新报告期": rows[0].get("period") or rows[0].get("date", ""),
        "最近报告": [
            {
                "报告期": row.get("period") or row.get("date", ""),
                "报告日期": row.get("date", ""),
                "营业收入_亿元": yi(row.get("revenue")),
                "营业收入同比%": row.get("revenue_yoy"),
                "归母净利润_亿元": yi(row.get("net_profit")),
                "归母净利润同比%": row.get("net_profit_yoy"),
                "扣非净利润_亿元": yi(row.get("net_profit_deduct")),
                "扣非净利润同比%": row.get("net_profit_deduct_yoy"),
                "基本每股收益": row.get("eps"),
                "ROE%": row.get("roe"),
                "毛利率%": row.get("gross_margin"),
                "负债率%": row.get("debt_ratio"),
            }
            for row in rows[:6]
        ],
        "分析提示": "同比指标用于跨期比较；Q1/H1/Q3/FY 口径不同，不将报告期绝对值直接横比。",
    }


def _payload_oscillators(osc: dict[str, Any]) -> dict[str, Any]:
    """MACD/KDJ 汇总投喂（数值 + 状态信号），供 LLM 技术面判断。"""
    macd, kdj = osc.get("macd") or {}, osc.get("kdj") or {}
    if not macd or not kdj:
        return {"说明": "K线不足（需≥35 根），MACD/KDJ 无法计算"}
    return {
        "MACD": {
            "DIF": macd.get("dif"),
            "DEA": macd.get("dea"),
            "柱": macd.get("hist"),
            "信号": macd.get("cross") or ("柱" + (macd.get("hist_trend") or "")) or "无",
            "柱状趋势": macd.get("hist_trend"),
        },
        "KDJ": {
            "K": kdj.get("k"),
            "D": kdj.get("d"),
            "J": kdj.get("j"),
            "信号": kdj.get("cross") or "无",
            "区域": kdj.get("zone"),
        },
    }


def _payload_intraday_signals(q: dict[str, Any]) -> list[dict[str, Any]]:
    """当日触发盘口信号 + 历史强度/命中率/置信度（供 LLM 解读时权衡样本可靠性）。

    与规则引擎 _intraday_score 同源（同一 note），保证双路径口径一致。
    """
    _pts, note = _intraday_score(q)
    out: list[dict[str, Any]] = []
    for it in _annotate_intraday(note):
        conf = it.get("confidence") or {}
        out.append({
            "信号": it["text"],
            "历史强度": it["strength"] or "未标注",
            "历史命中率": it["hit"] or "无",
            "置信度": conf.get("label", "-"),
            "置信度说明": conf.get("note", ""),
        })
    return out


def _intraday_score(q: dict[str, Any]) -> tuple[int, str]:
    """当日盘口分项：盘中位置×涨跌方向 + 量比 + 振幅 + 换手对技术面的修正。

    返回 (加分, 说明)。盘口数据缺失时返回 (0, "")，不影响其它分项。
    """
    price, prev = q.get("price"), q.get("prev_close")
    hi, lo = q.get("high"), q.get("low")
    chg = q.get("change_pct")
    if not (price and prev and hi and lo and hi > lo and chg is not None):
        return 0, ""
    pos = (price - lo) / (hi - lo) * 100          # 现价处于当日高低区间的相对位置 0-100
    amp = (hi - lo) / prev * 100                  # 当日振幅
    vr = q.get("volume_ratio")
    turnover = q.get("turnover")
    score = 0
    bits: list[str] = []

    # 盘中位置 × 涨跌方向：高位强势/冲高回落、低位弱势/空头衰竭
    # 权重校准依据（backtest_intraday.py 日线近似 + backtest_compare.py 盘中对照）：
    # · 收盘时点回测：高位强势命中率 43.5%、振幅收敛 42.2% 均低于基线 46%，看多收敛；
    #   低位下跌 53.8% 高于基线，看空加强。
    # · 盘中对照实验（14:00 真实 5 分钟线快照，240 样本）：高位强势盘中命中率 54.7%
    #   显著高于收盘 43.8%，日线近似低估了盘中实时信号，故盘中回调至 +3；
    #   振幅收敛盘中 41.7% 仍低于基线，撤销看多方向安全；低位下跌两时点差异不显著，
    #   维持 −4。
    if pos >= 75:
        if chg > 0:
            score += 3
            bits.append(f"现价运行至当日高位（{pos:.0f}%）且上涨，多头强势（注意冲高后回归压力）")
        else:
            score -= 4
            bits.append(f"现价自当日高位回落（{pos:.0f}%）转跌，短线抛压显现")
    elif pos <= 25:
        if chg < 0:
            score -= 4
            bits.append(f"现价贴近当日低位（{pos:.0f}%）且下跌，弱势明显")
        else:
            score += 2
            bits.append(f"现价自当日低位（{pos:.0f}%）回升，空头动能衰竭")

    # 量比：放量验证方向 / 缩量削弱信号
    if vr is not None:
        if vr >= 2:
            if chg > 0:
                score += 3
                bits.append(f"量比 {vr:.2f} 放量上攻，量价配合良好")
            else:
                score -= 3
                bits.append(f"量比 {vr:.2f} 放量下挫，抛压集中释放")
        elif vr <= 0.6:
            if chg > 0:
                score -= 1
                bits.append(f"量比 {vr:.2f} 缩量上涨，涨势动能存疑")
            else:
                score += 1
                bits.append(f"量比 {vr:.2f} 缩量下跌，抛压有所减轻")

    # 振幅：剧烈波动是风险；收敛不进分（回测显示振幅收敛次日上涨率 42.2% 反向，撤销看多）
    if amp >= 8:
        score -= 2
        bits.append(f"当日振幅 {amp:.1f}%，波动剧烈")
    elif amp <= 1.5:
        bits.append(f"当日振幅 {amp:.1f}%，走势收敛")

    # 换手：极高警惕分歧出货；极低交投清淡
    # 校准依据：收盘时点回测命中率 54.3% 有效 → −2；盘中对照实验 14:00 命中率 52.5%
    # （160 样本，高于收盘 47.0%），盘中场景更有效，回调至 −3。
    if turnover is not None:
        if turnover >= 10:
            score += (-2 if chg < 0 else 1)
            bits.append(f"换手率 {turnover:.1f}% 偏高，{'分歧出货风险' if chg < 0 else '交投活跃'}")
        elif turnover <= 0.8:
            score -= 3
            bits.append(f"换手率 {turnover:.1f}% 过低，交投清淡")

    return max(-8, min(8, score)), "；".join(bits)


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
    tech_score = 0
    above = ma_sum.get("above_count", 0)
    tech_score += (above - 2) * 8                  # 站上均线数量
    arrangement = ma_sum.get("arrangement", "")
    tech_score += {"多头排列": 18, "短期多头": 8, "空头排列": -18, "短期空头": -8}.get(arrangement, 0)
    for w, weight in ((5, 3), (10, 3), (20, 4), (60, 4)):
        slope = (ma.get(w) or {}).get("slope")
        tech_score += weight if slope == "上行" else (-weight if slope == "下行" else 0)

    chg20 = trend.get("chg_20d")
    if chg20 is not None:
        tech_score += 6 if chg20 > 0 else -6

    sr_state = sr.get("state", "")
    if "突破" in sr_state:
        tech_score += 8
    elif "跌破" in sr_state:
        tech_score -= 12

    # 乖离修正：现价偏离 MA20 过大时提示超买/超卖风险（避免追高杀跌）
    ma20v = (ma.get(20) or {}).get("value")
    deviation_note = ""
    if price and ma20v:
        dev_pct = (price - ma20v) / ma20v * 100
        if dev_pct > 8:
            tech_score -= 4
            deviation_note = f"现价较 MA20 乖离 {dev_pct:.1f}%（超买）"
        elif dev_pct < -8:
            tech_score += 4
            deviation_note = f"现价较 MA20 乖离 {dev_pct:.1f}%（超卖）"

    # 摆动指标（MACD/KDJ）：仅分析展示与 LLM 投喂，不参与评分与结论——
    # 当前市场行情下 MACD/KDJ 已不适合作为决策性数据（金叉/死叉/超买超卖
    # 频繁钝化失效），保留数值与状态描述供用户与模型参考。
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

    # 当日盘口分项（技术面修正）：盘中位置/量比/振幅/换手，实时盘口影响结论
    intraday_pts, intraday_note = _intraday_score(q)
    tech_score += intraday_pts

    # 资金面：当日主力为主 / 近5日辅 / 连续流向 / 两融 / 量能确认
    # 与详情页展示口径一致：用户看到的第一行是当日主力净额，评分以当日为准，
    # 近5日/30日累计仅作趋势参考（避免「当日流出却判流入」的矛盾观感）。
    # 当日资金流向未发布（盘中/收盘后16点前，fresh=False）时主项改用近5日口径，
    # 避免把前一交易日的 main_last 当成「当日」数据参与评分。
    capital_score = 0
    main_last = flow.get("main_last") or 0
    main_last5 = flow.get("main_last5") or 0
    main_total = flow.get("main_total") or 0
    flow_fresh = bool(flow.get("fresh", False))
    flow_last_date = flow.get("last_date") or ""
    primary = main_last if flow_fresh else main_last5
    secondary = main_last5 if flow_fresh else main_total  # 未发布时以 30 日累计方向作弱参考
    capital_score += 12 if primary > 0 else (-12 if primary < 0 else 0)
    capital_score += 6 if secondary > 0 else (-6 if secondary < 0 else 0)
    if flow.get("streak", 0) >= 3:
        capital_score += 6 if flow.get("streak_dir") == "流入" else -6

    rz_pct = margin.get("rz_change_pct")
    if rz_pct is not None:
        capital_score += 8 if rz_pct >= 5 else (-8 if rz_pct <= -5 else 0)

    # 量能确认：放量涨 / 放量跌 / 缩量跌 / 缩量涨（近6根K线，量价配合验证趋势真实性）
    volume_pts, volume_note = _volume_confirm(detail.get("kline", []))
    capital_score += volume_pts

    # 消息面：资讯 + 研报
    bull_n, bear_n, _ = _news_score(news)
    news_pts = max(-12, min(12, bull_n * 4 - bear_n * 4))      # 每条 +4 / -4 封顶 ±12
    bull_r, bear_r, _ = _reports_score(reports)
    report_pts = max(-15, min(15, bull_r * 5 - bear_r * 5))    # 每条 +5 / -5 封顶 ±15
    fundamental_pts, fundamental_notes, fundamental_text = _fundamental_score(financials)
    news_score = news_pts + report_pts

    # 三维权重（环境变量 / 设置页可配，默认 1.0）
    w = get_weights()
    w_tech = round(tech_score * w["tech"], 1)
    w_capital = round(capital_score * w["capital"], 1)
    w_news = round(news_score * w["news"], 1)
    score = round(w_tech + w_capital + w_news + fundamental_pts, 1)

    # ==================== 信号一致性 ====================
    # 三面方向一致 -> 提高置信度；方向冲突 -> 降置信度并把激进操作降一档
    def _dir(v: float) -> int:
        return 1 if v > 0 else (-1 if v < 0 else 0)

    dirs = [d for d in (_dir(tech_score), _dir(capital_score), _dir(news_score)) if d != 0]
    signal_conflict = len(set(dirs)) > 1          # 三面中有正有负
    signal_aligned = len(dirs) >= 2 and len(set(dirs)) == 1  # 至少两面同向且无反向
    signal_note = ""
    if signal_conflict:
        signal_note = "技术面/资金面/消息面方向不一致，信号背离，建议观望确认后再操作"
    elif signal_aligned:
        signal_note = "技术面/资金面/消息面方向一致，信号共振增强"

    if score >= 28:
        action = ACTIONS[0]
        position = "可持有 7 成以上仓位，回踩不破 MA10 可加仓"
    elif score >= 5:
        action = ACTIONS[1]
        position = "维持现有 5-7 成仓位，不加不减"
    elif score >= -22:
        action = ACTIONS[2]
        position = "仓位降至 3 成以下，反弹至压力位分批减"
    else:
        action = ACTIONS[3]
        position = "清空持仓，不留底仓"

    # 信号冲突时激进操作降一档（不做追高/杀跌的激进操作，等方向确认）
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

    # 当日盘中实时描述（供前端「当日盘中」行展示）
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

    # 当日盘口提示（机会/风险，与盘口分项同源）：按信号拆分并标注历史命中率
    # 强度（高/中/低），条目为 {text, strength, hit, note}，前端渲染徽标；
    # 非盘口条目保持纯字符串，前端两种结构兼容。
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
                45, min(92, 68 + int(abs(score) / 3) + (8 if signal_aligned else (-12 if signal_conflict else 0)))
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
            # 三维分面明细（已加权）与信号一致性（供前端展示与用户溯源）
            "scores": {
                "tech": w_tech,
                "capital": w_capital,
                "news": w_news,
                "fundamental": fundamental_pts,
                "intraday": intraday_pts,  # 当日盘口分项（已计入技术面）
                "total": score,
            },
            "weights": w,
            "signal": "conflict" if signal_conflict else ("aligned" if signal_aligned else "neutral"),
            "signal_note": signal_note,
        },
    }


# ------------------------------------------------------------------ 校验与兜底

def _sanitize(result: dict[str, Any], fallback: dict[str, Any], price: float | None) -> dict[str, Any]:
    """确保 LLM 输出满足需求 6.3 的硬约束，缺失项用规则引擎结果补齐。"""
    out: dict[str, Any] = {}
    for section in ("trend", "capital", "fundamental", "risk", "advice"):
        base = dict(fallback.get(section) or {})
        got = result.get(section)
        if isinstance(got, dict):
            for k, v in got.items():
                if v not in (None, "", [], {}):
                    base[k] = v
        out[section] = base

    advice = out["advice"]
    action = str(advice.get("action") or "").strip()
    if action not in ACTIONS:
        matched = next((a for a in ACTIONS if a[:2] in action), None)
        advice["action"] = matched or fallback["advice"]["action"]
        if not matched:
            advice["action_note"] = f"模型返回的建议「{action}」不在规定选项内，已回退规则引擎结论"

    # 价位合理性：偏离现价 50% 以上判为幻觉，回退规则值
    if price:
        for key in ("support", "resistance", "stop_loss", "take_profit"):
            v = advice.get(key)
            try:
                fv = float(v)
            except (TypeError, ValueError):
                advice[key] = fallback["advice"].get(key)
                continue
            if fv <= 0 or not (price * 0.5 <= fv <= price * 1.5):
                advice[key] = fallback["advice"].get(key)
            else:
                advice[key] = round(fv, 2)

    try:
        advice["confidence"] = max(0, min(100, int(float(advice.get("confidence", 70)))))
    except (TypeError, ValueError):
        advice["confidence"] = 70

    # 低置信度撤销激进建议：模型自己都不确定（confidence < 50）却给出
    # 「积极持仓/加仓」或「清仓离场」这类不可逆操作，是典型幻觉/过度自信，
    # 撤销为「持有观望」并标注，避免用户依据低置信结论做出激进操作。
    if advice["confidence"] < 50 and advice["action"] in (ACTIONS[0], ACTIONS[3]):
        advice["action_note"] = (
            f"模型置信度过低（{advice['confidence']}%），已撤销激进建议"
            f"「{advice['action']}」，改为持有观望"
        )
        advice["action"] = ACTIONS[1]

    risk = out["risk"]
    for key in ("opportunities", "risks"):
        val = risk.get(key)
        if isinstance(val, str):
            risk[key] = [val]
        elif not isinstance(val, list) or not val:
            risk[key] = fallback["risk"][key]
        else:
            # 含具体数字（依据）的条目优先，模型泛泛而谈的空话排后，再截断到 5 条
            risk[key] = sorted(
                [str(x) for x in val],
                key=lambda s: (0 if any(c.isdigit() for c in s) else 1),
            )[:5]
    return out


# ------------------------------------------------------------------ 入口

async def analyze(
    detail: dict[str, Any],
    news: list[dict[str, Any]] | None = None,
    reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """news/reports 为资讯与券商研报，财报从 detail.financials 读取并一并投喂。

    LLM 路径投喂全文，规则引擎按财报同比与质量指标做保守基本面评分。
    """
    financials = detail.get("financials") or {}
    fallback = rule_based(detail, news, reports, financials)
    price = detail["quote"].get("price")
    meta: dict[str, Any] = {
        "engine": "rule",
        "model": "内置规则引擎",
        "generated_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        "degraded_reason": "",
        "news_count": len(news or []),
        "reports_count": len(reports or []),
        "financials_count": len(financials.get("rows") or []),
    }

    if not llm.available():
        meta["degraded_reason"] = "未配置 LLM_API_KEY，使用内置规则引擎"
        return {"analysis": fallback, "meta": meta, "input": build_payload(detail, news, reports)}

    payload = build_payload(detail, news, reports)
    import json as _json

    user = (
        "请分析下面这只 A 股的持仓决策，严格按系统提示的 JSON 结构输出：\n\n"
        + _json.dumps(payload, ensure_ascii=False, indent=1)
    )
    try:
        raw, llm_meta = await llm.chat_json(SYSTEM_PROMPT, user)
        analysis = _sanitize(raw, fallback, price)
        meta.update(engine="llm", model=llm_meta.get("model"), usage=llm_meta.get("usage"))
        return {"analysis": analysis, "meta": meta, "input": payload}
    except llm.LLMError as exc:
        log.warning("LLM 分析失败，降级规则引擎：%s", exc)
        meta["degraded_reason"] = f"AI 服务调用失败（{exc}），已降级为内置规则引擎"
        return {"analysis": fallback, "meta": meta, "input": payload}
