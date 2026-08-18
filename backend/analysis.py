"""AI 分析：全维度数据投喂 + 结构化输出，附无 LLM 时的规则引擎降级。

输出严格对齐需求 6.3：行情趋势分析 / 资金与两融情绪分析 / 风险与机会拆解 /
明确持仓操作建议（四选一，禁止模糊话术），并补充支撑压力与介入离场区间。
"""
from __future__ import annotations

import logging
from typing import Any

from . import llm
from .utils import now, round2

log = logging.getLogger("analysis")

ACTIONS = ["积极持仓/加仓", "持有观望", "减仓规避", "清仓离场"]

SYSTEM_PROMPT = """你是一名 A 股量化分析师，擅长用均线技术形态、资金流向、两融数据与市场资讯做短中期持仓决策。

你会收到某只股票的全维度实时数据（JSON），其中包含「市场资讯」段（近一个月相关新闻与 AI 情绪标注）
与「券商观点」段（近一个月券商研报与 AI 情绪标注）。
请综合基本面与技术面判断：资讯与研报可作为辅助依据，但必须基于数据本身做判断，禁止编造数据中不存在的消息面信息；
若资讯/研报情绪明显（如连续利好/利空、机构集中买入评级），应在风险机会与操作建议中反映其权重，并在 reason 中提及。

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

    return {
        "基础数据": {
            "股票名称": q.get("name"),
            "股票代码": q.get("code"),
            "所属板块": detail.get("boards") or ([q.get("board")] if q.get("board") else []),
            "现价": q.get("price"),
            "昨收": q.get("prev_close"),
            "涨跌幅%": q.get("change_pct"),
            "今开": q.get("open"),
            "最高": q.get("high"),
            "最低": q.get("low"),
            "成交量_万手": round((q.get("volume") or 0) / 1e6, 2),
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
        },
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
            "当日主力净额_亿元": yi(detail.get("fund_flow", {}).get("summary", {}).get("main_last")),
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
        "当前状态标签": [
            f"{t['group']}：{t['label']}" for t in detail.get("status", {}).get("tags", [])
        ],
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


def rule_based(
    detail: dict[str, Any],
    news: list[dict[str, Any]] | None = None,
    reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """无 LLM（或 LLM 失败）时的确定性降级分析，输出结构完全一致。"""
    q = detail["quote"]
    ma = {m["window"]: m for m in detail.get("ma", [])}
    ma_sum = detail.get("ma_summary", {})
    flow = detail.get("fund_flow", {}).get("summary", {})
    margin = detail.get("margin", {}).get("summary", {})
    sr = detail.get("support_resistance", {})
    trend = detail.get("status", {}).get("trend", {})
    price = q.get("price") or 0.0

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

    # ---- 打分：技术面 + 资金面 + 两融
    score = 0
    above = ma_sum.get("above_count", 0)
    score += (above - 2) * 8                       # 站上均线数量
    arrangement = ma_sum.get("arrangement", "")
    score += {"多头排列": 18, "短期多头": 8, "空头排列": -18, "短期空头": -8}.get(arrangement, 0)
    for w, weight in ((5, 3), (10, 3), (20, 4), (60, 4)):
        slope = (ma.get(w) or {}).get("slope")
        score += weight if slope == "上行" else (-weight if slope == "下行" else 0)

    main_total = flow.get("main_total") or 0
    main_last5 = flow.get("main_last5") or 0
    score += 12 if main_total > 0 else (-12 if main_total < 0 else 0)
    score += 8 if main_last5 > 0 else (-8 if main_last5 < 0 else 0)
    if flow.get("streak", 0) >= 3:
        score += 6 if flow.get("streak_dir") == "流入" else -6

    rz_pct = margin.get("rz_change_pct")
    if rz_pct is not None:
        score += 8 if rz_pct >= 5 else (-8 if rz_pct <= -5 else 0)

    chg20 = trend.get("chg_20d")
    if chg20 is not None:
        score += 6 if chg20 > 0 else -6

    sr_state = sr.get("state", "")
    if "突破" in sr_state:
        score += 8
    elif "跌破" in sr_state:
        score -= 12

    # 资讯面权重：每条利好 +4 / 利空 -4，封顶 ±12（避免单一信息面左右全局）
    bull_n, bear_n, _ = _news_score(news)
    news_pts = max(-12, min(12, bull_n * 4 - bear_n * 4))
    score += news_pts

    # 券商研报面权重：机构专业判断权威性更高，每条利好 +5 / 利空 -5，封顶 ±15
    bull_r, bear_r, _ = _reports_score(reports)
    report_pts = max(-15, min(15, bull_r * 5 - bear_r * 5))
    score += report_pts

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
    if main_total > 0:
        opportunities.append(f"近30日主力累计净流入 {yi(main_total)}，其中超大单 {yi(flow.get('xl_total'))}")
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

    if above <= 1:
        risks.append(f"股价仅站上 {above} 条均线，MA20={mval(20)} 压制明显（{mpos(20)}）")
    if arrangement in ("空头排列", "短期空头"):
        risks.append(f"均线{arrangement}，短期反弹易在 MA20={mval(20)} 受阻")
    if main_total < 0:
        risks.append(f"近30日主力累计净流出 {yi(abs(main_total))}，流出天数 {flow.get('outflow_days', 0)} 天")
    if (flow.get("sm_total") or 0) > 0 and main_total < 0 and flow.get("tiered"):
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

    if not opportunities:
        opportunities.append(f"当前价 {price:.2f} 距 20 日低点 {sr.get('low_20')} 有安全边际，超跌反弹可期")
    if not risks:
        risks.append(f"上方 {resistance} 为 {sr.get('resistance_from') or '近期高点'}，突破前存在震荡消化需求")

    return {
        "trend": {
            "summary": f"{trend.get('label', '震荡整理')}，{arrangement or '均线交织'}",
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
        },
        "capital": {
            "summary": f"{flow.get('state', '资金观望')}，{margin.get('sentiment', '两融情绪平稳')}",
            "main_force": (
                f"近30日主力净额合计 {yi(main_total)}（流入 {flow.get('inflow_days', 0)} 天 / "
                f"流出 {flow.get('outflow_days', 0)} 天），近5日 {yi(main_last5)}，"
                f"当前连续{flow.get('streak_dir', '持平')} {flow.get('streak', 0)} 天，"
                + (f"超大单 {yi(flow.get('xl_total'))}、大单 {yi(flow.get('lg_total'))}，"
                   if flow.get("tiered") else f"超大单 {yi(flow.get('xl_total'))}，")
                + f"整体呈{flow.get('trend', '震荡反复')}。"
            ),
            "retail": (
                f"中单 {yi(flow.get('md_total'))}、小单（散户）{yi(flow.get('sm_total'))}，"
                + ("散户在主力流出时接盘，筹码由强手转弱手。"
                   if (flow.get("sm_total") or 0) > 0 and main_total < 0
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
        "risk": {"opportunities": opportunities[:4], "risks": risks[:4]},
        "advice": {
            "action": action,
            "reason": (
                f"综合评分 {score}：均线站上 {above}/4 条且{arrangement or '交织'}，"
                f"30日主力{yi(main_total)}，{margin.get('sentiment', '两融平稳')}，{sr_state}"
                + (f"，资讯面 {bull_n} 利好/{bear_n} 利空（计 {news_pts:+d} 分）" if (news or []) else "")
                + (f"，研报面 {bull_r} 利好/{bear_r} 利空（计 {report_pts:+d} 分）" if (reports or []) else "")
                + "。"
            ),
            "confidence": max(45, min(92, 68 + int(abs(score) / 3))),
            "position": position,
            "support": support,
            "resistance": resistance,
            "entry_zone": zone(support),
            "exit_zone": zone(resistance),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "horizon": "5-10 个交易日",
            "score": score,
        },
    }


# ------------------------------------------------------------------ 校验与兜底

def _sanitize(result: dict[str, Any], fallback: dict[str, Any], price: float | None) -> dict[str, Any]:
    """确保 LLM 输出满足需求 6.3 的硬约束，缺失项用规则引擎结果补齐。"""
    out: dict[str, Any] = {}
    for section in ("trend", "capital", "risk", "advice"):
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

    risk = out["risk"]
    for key in ("opportunities", "risks"):
        val = risk.get(key)
        if isinstance(val, str):
            risk[key] = [val]
        elif not isinstance(val, list) or not val:
            risk[key] = fallback["risk"][key]
        else:
            risk[key] = [str(x) for x in val][:5]
    return out


# ------------------------------------------------------------------ 入口

async def analyze(
    detail: dict[str, Any],
    news: list[dict[str, Any]] | None = None,
    reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """news/reports 为近一个月资讯与券商研报（含逐条情绪解读）。

    LLM 路径投喂全文，规则引擎分别按资讯/研报情绪加权。
    """
    fallback = rule_based(detail, news, reports)
    price = detail["quote"].get("price")
    meta: dict[str, Any] = {
        "engine": "rule",
        "model": "内置规则引擎",
        "generated_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        "degraded_reason": "",
        "news_count": len(news or []),
        "reports_count": len(reports or []),
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
