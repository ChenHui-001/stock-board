"""策略一：AI 分析「评分 → 四档建议」阈值有效性检验（面板事件研究）。

验证生产规则引擎里 28 / 5 / -22 三道阈值，是否真的把「该加仓」与「该减仓」
区分开；顺带看四档建议的分布是否失衡，以及评分本身（按分位数切档）是否有区分度。

形态：事件研究（不模拟仓位、不调 export_results）。每个「标的 × 信号日」为一个
事件，信号日收盘计算评分并分档，次日开盘买入、持有 N 日后收盘卖出。

数据约束（已实测确认，结果中必须披露）：
  - 可复现：技术面（均线/排列/斜率/支撑压力/乖离/盘口）、基本面（财报同比，
    用 InfoPublDate 防未来函数）
  - 不可得：资金面（历史主力资金 / 两融接口仅当日快照，历史区间返回空）、
    消息面（历史资讯 / 研报情绪无法无偏重构）
  → 回测版评分 = 技术面 + 基本面；资金面与消息面置零。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ..analysis.rule_engine import (  # 与生产同口径，避免回测与线上脱节
    FACTOR_WEIGHTS, TH_BUY, TH_SELL, _damp, _round_half_away,
)
from . import engine, render
import logging
log = logging.getLogger("backtest.score_strategy")

STRATEGY_ID = "score_threshold"
STRATEGY_NAME = "AI 评分阈值检验"

ORDER = ["加仓", "观望", "减仓", "清仓"]
QORDER = ["Q1 低分", "Q2", "Q3", "Q4 高分"]

DEFAULT_CODES = [
    "sh601179",  # 中国西电
    "sh600000",  # 浦发银行
    "sh600519",  # 贵州茅台
    "sh600036",  # 招商银行
    "sh601899",  # 紫金矿业
    "sz000001",  # 平安银行
    "sz000333",  # 美的集团
    "sz000858",  # 五粮液
    "sz002594",  # 比亚迪
    "sz300750",  # 宁德时代
]

PARAMS_SCHEMA: list[dict[str, Any]] = [
    {
        "key": "codes", "label": "股票池", "type": "textarea",
        "default": ",".join(DEFAULT_CODES),
        "hint": "westock 格式（sh600000 / sz000001），逗号或换行分隔；也支持直接填 6 位代码",
    },
    {
        "key": "limit", "label": "日线根数", "type": "number",
        "default": 800, "min": 200, "max": 2000, "step": 50,
        "hint": "每只标的取多少根前复权日线；800 根约 3 年",
    },
    {
        "key": "th_add", "label": "加仓阈值", "type": "number",
        "default": TH_BUY, "min": -60, "max": 120, "step": 1,
    },
    {
        "key": "th_hold", "label": "观望阈值", "type": "number",
        "default": 0.0, "min": -60, "max": 60, "step": 1,
    },
    {
        "key": "th_reduce", "label": "减仓阈值", "type": "number",
        "default": TH_SELL, "min": -120, "max": 60, "step": 1,
    },
]


# ------------------------------------------------------------------ 评分复现

def compute_tech(df: pd.DataFrame) -> pd.DataFrame:
    """复现生产规则引擎的技术面：均线结构 + 支撑压力 + 乖离 + 盘口。"""
    d = df.copy()
    for w in (5, 10, 20, 60):
        d[f"ma{w}"] = d["close"].rolling(w).mean()
    d["prev_close"] = d["close"].shift(1)
    d["high20"] = d["high"].rolling(20).max()
    d["low20"] = d["low"].rolling(20).min()
    # 量比分母 = 前 5 日均量（严格不含当日，避免把当日量算进基准而系统性低估量比）
    d["vol_ma5"] = d["volume"].shift(1).rolling(5).mean()
    return d


def intraday_score(d: pd.DataFrame, i: int) -> float:
    """复现 _intraday_score：盘中位置×涨跌 + 量比 + 振幅 + 换手（clamp ±8）。"""
    row = d.iloc[i]
    price, prev = row["close"], row["prev_close"]
    hi, lo = row["high"], row["low"]
    chg = None
    if pd.notna(price) and pd.notna(prev) and prev:
        chg = (price - prev) / prev * 100
    if not (pd.notna(price) and pd.notna(prev) and pd.notna(hi)
            and pd.notna(lo) and hi > lo and chg is not None):
        return 0.0

    pos = (price - lo) / (hi - lo) * 100
    amp = (hi - lo) / prev * 100
    vr = row["volume"] / row["vol_ma5"] if (pd.notna(row["vol_ma5"]) and row["vol_ma5"]) else None
    turnover = row.get("turnover")
    turnover = turnover if (turnover is not None and pd.notna(turnover)) else None
    s = 0
    if pos >= 75:
        s += 3 if chg > 0 else -4
    elif pos <= 25:
        s += 2 if chg > 0 else -4
    if vr is not None:
        if vr >= 2:
            s += 3 if chg > 0 else -3
        elif vr <= 0.6:
            s += -1 if chg > 0 else 1
    if amp >= 8:
        s -= 2
    if turnover is not None:
        if turnover >= 10:
            s += 1 if chg > 0 else -2
        elif turnover <= 0.8:
            s -= 3
    return max(-8, min(8, s))


def tech_score(d: pd.DataFrame, i: int) -> float:
    """第 i 根 bar 的技术面得分（严格只用 i 及之前的数据）。"""
    row = d.iloc[i]
    close = row["close"]
    if pd.isna(close):
        return 0.0

    score = 0.0
    above = 0
    for w in (5, 10, 20, 60):
        mav = row[f"ma{w}"]
        if pd.notna(mav) and close > mav:
            above += 1
    score += (above - 2) * 8

    ma5, ma10, ma20, ma60 = (row["ma5"], row["ma10"], row["ma20"], row["ma60"])
    if all(pd.notna(x) for x in (ma5, ma10, ma20, ma60)):
        if ma5 > ma10 > ma20 > ma60:
            score += 18
        elif ma5 > ma10 > ma20:
            score += 8
        elif ma5 < ma10 < ma20 < ma60:
            score -= 18
        elif ma5 < ma10 < ma20:
            score -= 8

    for w, weight in ((5, 3), (10, 3), (20, 4), (60, 4)):
        if i >= 5:
            prev = d.iloc[i - 5][f"ma{w}"]
            cur = row[f"ma{w}"]
            if pd.notna(prev) and pd.notna(cur):
                if cur > prev:
                    score += weight
                elif cur < prev:
                    score -= weight

    if i >= 20 and pd.notna(d.iloc[i - 20]["close"]) and d.iloc[i - 20]["close"]:
        chg20 = (close - d.iloc[i - 20]["close"]) / d.iloc[i - 20]["close"] * 100
        score += 6 if chg20 > 0 else -6

    if i >= 21:
        prev_high = d.iloc[i - 20:i]["high"].max()
        prev_low = d.iloc[i - 20:i]["low"].min()
        if pd.notna(prev_high) and close > prev_high:
            score += 8
        elif pd.notna(prev_low) and close < prev_low:
            score -= 12

    if pd.notna(ma20) and ma20:
        dev = (close - ma20) / ma20 * 100
        if dev > 8:
            score -= 4
        elif dev < -8:
            score += 4

    score += intraday_score(d, i)
    return score


def fundamental_score(fins: list[dict[str, Any]], signal_date: str) -> float:
    """用「信号日已发布」的最新财报计算同比分（营收/净利各 ±2，clamp ±8）。"""
    known = [f for f in fins if f["pub_date"] <= signal_date]
    if not known:
        return 0.0
    latest = known[-1]
    end_date = latest["end_date"]
    prev_year = str(int(end_date[:4]) - 1) + end_date[4:]
    prev = next((f for f in known if f["end_date"] == prev_year), None)
    score = 0.0
    for key in ("revenue", "net_profit"):
        cur = latest.get(key)
        base = (prev or {}).get(key)
        if cur is None or base is None or not base:
            continue
        yoy = (cur - base) / abs(base) * 100
        if yoy >= 5:
            score += 2
        elif yoy <= -5:
            score -= 2
    return max(-8, min(8, score))


# ------------------------------------------------------------------ 分档

def bucket_by_threshold(score: float, th: dict[str, float]) -> str:
    if score >= th["add"]:
        return "加仓"
    if score >= th["hold"]:
        return "观望"
    if score >= th["reduce"]:
        return "减仓"
    return "清仓"


def bucket_by_quantile(score: float, q: dict[str, float]) -> str:
    if score >= q["q75"]:
        return "Q4 高分"
    if score >= q["q50"]:
        return "Q3"
    if score >= q["q25"]:
        return "Q2"
    return "Q1 低分"


# ------------------------------------------------------------------ 主流程

def build_events(
    codes: list[str], limit: int, on_progress: Callable[[float, str], None]
) -> pd.DataFrame:
    """逐标的构造事件表（同步 CPU 密集，放在线程池里跑）。"""
    events: list[dict[str, Any]] = []
    total = len(codes)
    for idx, code in enumerate(codes, 1):
        on_progress(idx / max(1, total) * 0.85, f"计算 {code}（{idx}/{total}）")
        try:
            df = engine.load_kline(code, limit)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{code}] 日线获取失败: {exc}")
            continue
        if df is None or len(df) < engine.WARMUP_BARS + 20:
            print(f"  [{code}] 数据不足，跳过")
            continue
        try:
            fins = engine.load_fundamentals(code)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 异常，按空数据继续: %s", "build_events", exc)
            fins = []
        d = compute_tech(df)
        n = len(d)
        for i in range(engine.WARMUP_BARS, n):
            date = str(d.iloc[i]["date"])[:10]
            t_score = tech_score(d, i)
            f_score = fundamental_score(fins, date)
            ev: dict[str, Any] = {
                "symbol": code,
                "signal_date": date,
                "tech_score": round(t_score, 1),
                "fundamental_score": round(f_score, 1),
                "score": round(t_score + f_score, 1),
            }
            if i + 1 >= n:
                continue
            buy_price = d.iloc[i + 1]["open"]
            if pd.isna(buy_price) or not buy_price:
                continue
            ev["buy_date"] = str(d.iloc[i + 1]["date"])[:10]
            ev["buy_price"] = round(float(buy_price), 3)
            ok = False
            for nd in engine.FORWARD_DAYS:
                j = i + 1 + nd - 1 if nd == 1 else i + nd
                if j >= n:
                    ev[f"fwd{nd}"] = None
                    continue
                sell = d.iloc[j]["close"]
                if pd.isna(sell):
                    ev[f"fwd{nd}"] = None
                    continue
                ev[f"fwd{nd}"] = round((sell / buy_price - 1) * 100, 3)
                ev[f"sell{nd}_date"] = str(d.iloc[j]["date"])[:10]
                ok = True
            if ok:
                events.append(ev)
        print(f"  [{code}] 完成，累计事件 {len(events)} 条")
    return pd.DataFrame(events)


def _yearly_rows(trades: pd.DataFrame) -> list[dict[str, Any]]:
    """分年度稳健性：加仓档 vs 清仓档 5 日胜率差。"""
    if trades.empty or "signal_date" not in trades.columns:
        return []
    t = trades.copy()
    t["year"] = t["signal_date"].astype(str).str[:4]
    rows: list[dict[str, Any]] = []
    for y in sorted(t["year"].unique()):
        yd = t[t["year"] == y]
        a = yd[yd["档位_阈值"] == "加仓"]["pnl_pct"]
        c = yd[yd["档位_阈值"] == "清仓"]["pnl_pct"]
        if len(a) < 20 or len(c) < 20:
            rows.append({"年份": f"{y} 年", "加仓样本": len(a), "清仓样本": len(c),
                         "加仓胜率": "—", "清仓胜率": "—", "差值": "样本不足", "_raw": 0})
            continue
        wa, wc = (a > 0).mean() * 100, (c > 0).mean() * 100
        rows.append({
            "年份": f"{y} 年", "加仓样本": len(a), "清仓样本": len(c),
            "加仓胜率": f"{wa:.1f}%", "清仓胜率": f"{wc:.1f}%",
            "差值": f"{wa - wc:+.1f}pct", "_raw": round(float(wa - wc), 1),
        })
    return rows


async def run(params: dict[str, Any], on_progress: Callable[[float, str], None]) -> dict[str, Any]:
    """策略入口：返回标准结果 dict（由 store.finish 落盘）。"""
    raw_codes = params.get("codes") or ",".join(DEFAULT_CODES)
    codes = [engine.to_westock_symbol(c) for c in engine.normalize_codes(raw_codes)]
    if not codes:
        raise ValueError("股票池为空，请至少填写一只标的代码")
    limit = int(params.get("limit") or 800)
    limit = max(200, min(2000, limit))
    th = {
        "buy":  float(params.get("th_buy", TH_BUY)),
        "sell": float(params.get("th_sell", TH_SELL)),
    }

    on_progress(0.02, f"准备 {len(codes)} 只标的 × {limit} 根日线")
    df = await _to_thread(build_events, codes, limit, on_progress)
    if df.empty:
        raise RuntimeError("无有效事件：可能是日线数据不足或股票池代码无效")

    # 只保留前瞻窗口完整的事件：末尾几根 K 线没有未来 5/10 日数据，
    # 收益为空的行若参与统计会污染样本数与占比
    before = len(df)
    df = df[df[f"fwd{engine.PRIMARY_DAYS}"].notna()].reset_index(drop=True)
    dropped = before - len(df)

    q = {
        "q25": float(df["score"].quantile(0.25)),
        "q50": float(df["score"].quantile(0.50)),
        "q75": float(df["score"].quantile(0.75)),
    }
    df["档位_阈值"] = df["score"].map(lambda s: bucket_by_threshold(s, th))
    df["档位_分位"] = df["score"].map(lambda s: bucket_by_quantile(s, q))

    out = df.copy()
    out["pnl_pct"] = out[f"fwd{engine.PRIMARY_DAYS}"]
    out["holding_days"] = engine.PRIMARY_DAYS
    out["label"] = out["档位_阈值"] + "档（评分 " + out["score"].astype(str) + "）"
    out["entry_date"] = out["buy_date"]
    out["exit_date"] = out.get(f"sell{engine.PRIMARY_DAYS}_date")
    out["entry_price"] = out["buy_price"]
    cols = ["symbol", "signal_date", "entry_date", "exit_date", "entry_price",
            "score", "tech_score", "fundamental_score", "档位_阈值", "档位_分位",
            "pnl_pct", "holding_days", "label"]
    out = out[cols]

    s_threshold = engine.summarize_by_bucket(df, "档位_阈值", ORDER)
    s_quantile = engine.summarize_by_bucket(df, "档位_分位", QORDER)
    stats = engine.event_stats(df[f"fwd{engine.PRIMARY_DAYS}"])

    tmpdir = Path(tempfile.mkdtemp(prefix="bt_score_", dir=str(engine.CACHE_DIR)))
    trades_csv = tmpdir / "trades.csv"
    out.to_csv(trades_csv, index=False, encoding="utf-8-sig")

    meta = {
        "strategy_name": f"{STRATEGY_NAME}（事件研究）",
        "symbol": f"{len(codes)} 只 A 股面板",
        "start": str(df["signal_date"].min())[:10],
        "end": str(df["signal_date"].max())[:10],
        "note": "回测版评分=技术面+基本面；资金面与消息面历史数据不可得，置零",
    }
    full_summary = {
        "meta": meta,
        "summary": {**stats, "quantile_cuts": {k: round(v, 2) for k, v in q.items()},
                    "dropped_events": dropped},
        "bucket_threshold": s_threshold.to_dict(orient="records"),
        "bucket_quantile": s_quantile.to_dict(orient="records"),
    }

    on_progress(0.92, "生成看板")
    extra = _dashboard_modules(s_threshold, s_quantile, out, th)
    report_html = tmpdir / "report.html"
    render.render_report(
        output_path=report_html,
        trades_csv=trades_csv,
        meta=meta,
        summary=stats,
        extra_modules=extra,
        tabs=[
            {"id": "overview", "label": "阈值检验"},
            {"id": "robust", "label": "稳健性与建议"},
            {"id": "trades", "label": "事件明细（抽样）"},
        ],
        trade_columns=[
            {"key": "symbol", "label": "标的"},
            {"key": "signal_date", "label": "信号日"},
            {"key": "entry_date", "label": "买入日"},
            {"key": "exit_date", "label": "卖出日"},
            {"key": "entry_price", "label": "买入价", "format": "number"},
            {"key": "score", "label": "评分", "format": "number"},
            {"key": "档位_阈值", "label": "档位", "format": "pill"},
            {"key": "pnl_pct", "label": "5日收益", "format": "pct"},
        ],
        group_col="档位_阈值",
        sample_title="事件明细（每档抽样 120 条，完整数据在 CSV）",
    )
    on_progress(1.0, "完成")

    return {
        "summary": stats,
        "full_summary": full_summary,
        "tables": {
            "bucket_threshold": s_threshold.to_dict(orient="records"),
            "bucket_quantile": s_quantile.to_dict(orient="records"),
            "yearly": _yearly_rows(out),
        },
        "trades_csv": trades_csv,
        "report_html": report_html,
        "meta": meta,
    }


async def _to_thread(fn: Callable[..., Any], *args: Any) -> Any:
    """把同步 CPU 密集计算放进线程，避免阻塞事件循环。"""
    import asyncio

    return await asyncio.to_thread(fn, *args)


# ------------------------------------------------------------------ 看板模块

def _dashboard_modules(
    s_threshold: pd.DataFrame, s_quantile: pd.DataFrame, trades: pd.DataFrame,
    th: dict[str, float],
) -> list[dict[str, Any]]:
    def pick(df: pd.DataFrame, bucket: str, col: str, default: float = 0.0) -> float:
        sub = df[df["档位"] == bucket]
        if sub.empty or col not in sub.columns:
            return default
        return float(sub.iloc[0][col])

    add_mean, add_win = pick(s_threshold, "加仓", "5日均值%"), pick(s_threshold, "加仓", "5日胜率%")
    clr_mean, clr_win = pick(s_threshold, "清仓", "5日均值%"), pick(s_threshold, "清仓", "5日胜率%")
    hold_pct = pick(s_threshold, "观望", "占比%")

    conclusion = (
        f"- **阈值 {th['add']:.0f} / {th['hold']:.0f} / {th['reduce']:.0f} 的分档效果**："
        f"加仓档信号后 5 日平均收益 {add_mean:+.3f}%、胜率 {add_win:.1f}%；"
        f"清仓档 {clr_mean:+.3f}%、胜率 {clr_win:.1f}%。\n"
        f"- 分位数切档用于检验评分本身的区分度（见「稳健性与建议」）。\n"
        f"- 中性档「观望」占比 {hold_pct:.1f}%，若远低于两端说明建议分布两极分化。"
    )
    return [
        {"type": "text", "tab": "overview", "title": "结论", "text": conclusion},
        {
            "type": "custom_html", "tab": "overview", "width": "full",
            "title": "各档位信号后 5 日收益对比",
            "html": render.svg_bar_chart(s_threshold.to_dict(orient="records")),
        },
        {
            "type": "metric_table", "tab": "overview",
            "title": "阈值分档统计（信号后 5/10 日）",
            "columns": ["档位", "样本数", "占比", "5日均值", "5日中位", "5日胜率", "10日胜率"],
            "rows": render.metric_rows(s_threshold, [
                ("样本数", False), ("占比%", False), ("5日均值%", False),
                ("5日中位%", False), ("5日胜率%", True), ("10日胜率%", True),
            ]),
        },
        {
            "type": "text", "tab": "overview", "title": "关键假设",
            "text": (
                "- 信号在当日收盘后计算，次日开盘买入、持有 N 个交易日后收盘卖出，杜绝未来函数。\n"
                "- 回测版评分 = 技术面 + 基本面。**资金面与消息面历史数据不可得，置零处理**："
                "历史主力资金与两融接口仅提供当日快照，历史区间返回空；"
                "历史资讯/研报情绪无法在回测时点无偏重构。\n"
                "- 盘口信号（盘中位置、量比、振幅、换手）用日线在收盘时点近似，"
                "量比 = 当日成交量 / 前 5 日均量（严格不含当日）。\n"
                "- 基本面使用「信号日已发布」的最新财报（按披露日过滤），避免提前使用未公开财报。\n"
                "- 事件研究不计手续费与滑点；不模拟仓位，直接统计价格变动百分比。\n"
                "- 均线预热 90 根 K 线后才开始产生事件，确保 MA60 有效。"
            ),
        },
        {
            "type": "text", "tab": "robust", "title": "局限与已知偏差",
            "text": (
                "- **幸存者偏差**：股票池为当前仍在交易的大盘股，已退市或长期走弱标的未纳入，"
                "结论可能偏乐观（即真实区分度可能更差）。\n"
                "- **事件重叠**：日度事件的前瞻窗口相互重叠，胜率的标准误被低估。\n"
                "- **时点近似**：生产策略是盘中决策，回测用收盘快照近似，"
                "若信号存在日内 alpha 则会被低估；但均线主体在收盘计算无误。\n"
                "- **未复现维度**：资金面（±38）与消息面（±27）因数据不可得未参与评分，"
                "本结论检验的是「技术面主导的评分体系」——而技术面恰是生产决策的主要驱动力。"
            ),
        },
        {
            "type": "metric_table", "tab": "robust",
            "title": "分位数分档（检验评分本身是否有区分度）",
            "columns": ["分组", "样本数", "5日均值", "5日中位", "5日胜率"],
            "rows": render.metric_rows(s_quantile, [
                ("样本数", False), ("5日均值%", False), ("5日中位%", False), ("5日胜率%", True),
            ]),
        },
        {
            "type": "metric_table", "tab": "robust",
            "title": "分年度稳定性（加仓档 vs 清仓档 5 日胜率差）",
            "columns": ["年份", "加仓样本", "清仓样本", "加仓胜率", "清仓胜率", "差值"],
            "rows": [
                {
                    "metric": r["年份"],
                    "values": [
                        {"main": str(r["加仓样本"])}, {"main": str(r["清仓样本"])},
                        {"main": r["加仓胜率"]}, {"main": r["清仓胜率"]},
                        {"main": r["差值"], "raw": r["_raw"]},
                    ],
                }
                for r in _yearly_rows(trades)
            ],
        },
        {
            "type": "text", "tab": "robust", "title": "优化建议",
            "text": (
                "- 现有评分（均线主导）若未通过有效性检验，建议改用资金/情绪/相对强度主导的体系："
                "主力资金净比、量价分层、板块情绪周期、个股相对板块强弱、价格与时间双止损。\n"
                "- 若保留均线维度，应大幅降低其权重，并用回测重新标定阈值，而非沿用经验值。\n"
                "- 信号可靠性表中样本量极低（n<10）的信号应停止参与打分。\n"
                "- 建议补充「资金面」历史数据源（当前接口仅当日），否则该维度无法被验证。"
            ),
        },
    ]
