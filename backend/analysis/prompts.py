"""投喂给 LLM 的数据：系统提示词 + 详情页数据压缩。

本模块职责：
  1. 维护 SYSTEM_PROMPT + FEWSHOT_EXAMPLE + ACTIONS 等 LLM 接口契约；
  2. 把 `service.stock_detail()` 返回的大字典压缩成 LLM 友好的紧凑 JSON。

依赖：
  - 反向 import `rule_engine._intraday_score` 与 `rule_engine._annotate_intraday`，
    因为盘口信号本身是规则计算出来的，prompt 只是把它喂给 LLM。
"""
from __future__ import annotations

import logging
from typing import Any

from ..utils import describe_exc
from .rule_engine import _annotate_intraday, _intraday_score

log = logging.getLogger("analysis.prompts")

# P0-5：3 档（加仓/减仓/观望），与 rule_engine.ACTIONS 保持一致
ACTIONS = ["加仓", "减仓", "观望"]

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
3. action 字段必须严格取以下三个字符串之一，不得改写：
   "加仓"、"减仓"、"观望"
   有且仅有一个高亮，绝不输出「可加可减」「逢低关注」类模糊表述；
   「观望」指维持现有仓位不动等待方向确认，是明确的不操作指令。
4. 所有价位必须是具体数字，精确到 2 位小数，且落在合理价格区间内。
5. 每段分析都要引用具体数值（均线值、乖离率、资金金额、融资余额变化等）作为依据。
6. 必须额外输出 `confidence_reason` 字段，用一句话说明为何给出当前置信度
   （如：三面同向/背离、信号样本量、关键数据缺失等），让用户能溯源。

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
    "horizon": "建议持有周期，如：5-10 个交易日",
    "confidence_reason": "一句话说明置信度依据，如：三面同向/背离、样本量、数据缺失"
  }
}"""


# 精简 few-shot：1 个结构示例（无真实数据），目的不是教模型做决策，而是把 schema
# 锚定住——减少 action 字段被改写成"建议加仓（请注意风险）"、字段错位等格式漂移。
# 仅在零样本下偶发格式问题；该示例由量化分析师测试稳定下来后可移除。
FEWSHOT_EXAMPLE = """
【输出示例（仅示意结构，数值与代码无对应关系）】
{
 "trend": {
   "summary": "多头排列后回踩 MA10",
   "short": "近5日+3.2%，MA5=12.80 上行、MA10=12.65，股价站上 MA5，短线偏多",
   "mid": "近20日+6.5%，MA20=12.45 上行，股价站上 MA20，中期趋势向好",
   "long": "近60日+11.2%，MA60=11.90 上行，股价站上季线，中长期多头格局",
   "pattern": "多头排列后回踩 MA10 不破，缩量企稳"
 },
 "capital": {
   "summary": "主力连续 3 日净流入",
   "main_force": "近5日主力净流入 0.85 亿，近30日累计 2.30 亿，超大单占比 62%",
   "retail": "中单+0.10 亿、小单-0.30 亿，散户资金与主力反向",
   "margin": "融资余额 30 日变动 +4.8%，杠杆资金做多意愿抬升"
 },
 "fundamental": {
   "summary": "营收净利双增",
   "period": "2026H1",
   "growth": "营收同比 +18.2%、归母净利润同比 +24.5%",
   "quality": "ROE 12.3%、毛利率 28.4%、负债率 42.1%"
 },
 "risk": {
   "opportunities": [
     "MA5/10/20 多头排列，MA20=12.45 构成有效支撑",
     "近5日主力净流入 0.85 亿，资金面配合"
   ],
   "risks": [
     "散户资金与主力反向，需警惕拉高出货",
     "上方压力位 13.80（近期高点）突破前存在震荡消化"
   ]
 },
 "advice": {
   "action": "加仓",
   "reason": "三面同向：均线多头+融资抬升+业绩双增，MA20=12.45 不破可持有",
   "confidence": 78,
   "confidence_reason": "三面同向共振但 30 日资金样本量有限，且散户反向，置信度略低于顶部",
   "position": "可持有 7 成以上，回踩 MA10 不破可加仓",
   "support": 12.45,
   "resistance": 13.80,
   "entry_zone": "12.60-12.90",
   "exit_zone": "13.50-13.90",
   "stop_loss": 12.10,
   "take_profit": 14.10,
   "horizon": "5-10 个交易日"
 }
}"""


def system_prompt() -> str:
    """动态组装 SYSTEM_PROMPT：few-shot 示例集中放在尾部，避免与硬性要求混淆。"""
    return SYSTEM_PROMPT + FEWSHOT_EXAMPLE


# ------------------------------------------------------------------ 资讯/研报相关性排序

# 加权威媒体权重（彭博/财联社等），避免低质媒体抢位。
_RELEVANCE_MEDIA_BONUS = {
    "新浪财经": 1.0, "财联社": 1.2, "证券时报": 1.2, "上海证券报": 1.2,
    "证券日报": 1.1, "中国证券报": 1.2, "21世纪经济报道": 1.1,
    "第一财经": 1.1, "澎湃新闻": 1.0, "经济观察报": 1.1, "界面新闻": 1.0,
    "华尔街见闻": 1.1, "36氪": 0.9, "钛媒体": 0.9, "虎嗅": 0.9,
}


def _relevance_score(item: dict, name: str, code: str) -> float:
    """资讯/研报与该股的相关性评分：标题/摘要出现股票名或代码给高分，
    权威媒体给权重加成；用于排序后投喂给 LLM，让头几条就是最相关的内容。
    无股票名/代码命中的条目仍按时间倒序保留，不会被全淘汰。
    """
    title = item.get("title", "") or ""
    summary = item.get("summary", "") or ""
    text = title + " " + summary
    score = 0.0
    if name and name in text:
        score += 8 + (5 if name in title else 0)
    if code and code in text:
        score += 6 + (4 if code in title else 0)
    src = item.get("source", "") or ""
    score *= _RELEVANCE_MEDIA_BONUS.get(src, 1.0)
    return score


def _sort_by_relevance(
    items: list,
    name: str,
    code: str,
    *,
    limit: int,
) -> list:
    """按相关性倒序排序后截断。保留所有条目，仅在排序上让最相关的优先进入截断窗口；
    同分按时间倒序（保持调用方预期）。
    """
    if not items:
        return []
    scored = sorted(
        items,
        key=lambda x: (
            _relevance_score(x, name, code),
            x.get("date", "") or "",
        ),
        reverse=True,
    )
    return scored[:limit]


# ------------------------------------------------------------------ payload 压缩片段

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


def _atr_breakout_note(sr: dict[str, Any]) -> str:
    """ATR 突破判定的语义化解读，便于 LLM 在风险/机会描述里引用。

    输入是 support_resistance() 输出：
      atr         = 当前 ATR(14) 数值
      atr_breakout = "已突破"/"已跌破"/"逼近"/"未触及"
      state       = 文字标签
    """
    atr = sr.get("atr")
    state = sr.get("state") or ""
    flag = sr.get("atr_breakout") or "未触及"
    if not atr:
        return "ATR 不可用，支撑压力按固定比例阈值判定（兼容旧逻辑）"
    if flag in ("已突破", "已跌破"):
        return f"{state}，ATR(14)={atr}，突破幅度已超过 0.5 倍 ATR 容差，可信度较高"
    if flag == "逼近":
        return f"{state}，ATR(14)={atr}，距突破仅 0.5 倍 ATR 内，关注是否有效突破"
    return f"运行于区间内部，ATR(14)={atr}，未触及压力/支撑边界"


def _payload_intraday_signals(q: dict[str, Any]) -> list[dict[str, Any]]:
    """当日触发盘口信号 + 历史强度/命中率/置信度（供 LLM 解读时权衡样本可靠性）。

    与规则引擎 `_intraday_score` 同源（同一 note），保证双路径口径一致。
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


# ------------------------------------------------------------------ build_payload 主入口

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

    # 抽出 trend / vol_atr 复用：避免链式 .get().get() 满天飞
    trend = detail.get("status", {}).get("trend") or {}
    vol_atr = trend.get("vol_unit_atr") or {}
    atr = trend.get("atr")

    # ATR14 占股价% 是 build_payload 里唯一一处需要做除法的字段，
    # 价格缺失 / ATR 异常等情况必须降级为 None，否则会再次发生类似 NameError
    # 的隐藏 bug 把整次 AI 分析拖崩。
    atr_pct: float | None = None
    if atr and price:
        try:
            atr_pct = round(float(atr) / float(price) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            log.warning("build_payload: ATR14占股价%% 计算异常已降级 None: %s", describe_exc(exc))

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
            "盘中位置%": intraday_pos,
            "量比": q.get("volume_ratio"),
            "成交量_万手": round((q.get("volume") or 0) / 1e6, 2),
            "近60日均量_万手": avg_vol_60,
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
            "近5日涨跌幅%": trend.get("chg_5d"),
            "近20日涨跌幅%": trend.get("chg_20d"),
            "近60日涨跌幅%": trend.get("chg_60d"),
            "ATR14": atr,
            "ATR14占股价%": atr_pct,
            "近5日波幅_单位ATR": vol_atr.get("chg_5d"),
            "近20日波幅_单位ATR": vol_atr.get("chg_20d"),
            "近60日波幅_单位ATR": vol_atr.get("chg_60d"),
            "是否ATR归一化判定": trend.get("atr_normalized"),
            "近1日量_5日均量比": trend.get("vol_5d_ratio"),
            "量能验证": trend.get("volume_confirm"),
            "近30日收盘序列": [round(b["close"], 2) for b in bars[-30:]],
            "近60日收盘序列": [round(b["close"], 2) for b in bars_60],
        },
        "技术指标_MACD_KDJ": _payload_oscillators(detail.get("oscillators") or {}),
        "支撑压力": {
            **detail.get("support_resistance", {}),
            "ATR突破解读": _atr_breakout_note(detail.get("support_resistance", {})),
        },
        "资金数据_近30日": {
            **{k: v for k, v in detail.get("fund_flow", {}).get("summary", {}).items()
               if k in ("trend", "state", "inflow_days", "outflow_days",
                        "streak", "streak_dir", "streak_text",
                        "state_grade",
                        "price_flow_note", "xl_dominance")},
            "价量背离信号": detail.get("fund_flow", {}).get("summary", {}).get("price_flow_note"),
            "主力类型分类": detail.get("fund_flow", {}).get("summary", {}).get("xl_dominance"),
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
            "情绪含披露日期": detail.get("margin", {}).get("summary", {}).get("sentiment_with_date"),
            "最新披露日期": detail.get("margin", {}).get("summary", {}).get("last_date"),
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
            for n in _sort_by_relevance(news or [], q.get("name") or "", q.get("code") or "", limit=15)
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
            for r in _sort_by_relevance(reports or [], q.get("name") or "", q.get("code") or "", limit=10)
        ],
    }
