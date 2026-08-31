"""AI 分析策略「评分 → 四档建议」阈值有效性回测（面板事件研究）。

目的：验证生产规则引擎里 28 / 5 / -22 这三道阈值，是否真的把「该加仓」和
「该减仓」区分开；顺带看四档建议的分布是否失衡。

形态：事件研究（不模拟仓位、不调 export_results）。每个「标的 × 信号日」为
一个事件，在信号日收盘计算评分并分档，用「次日开盘买入、持有 N 日后收盘卖出」
计算前瞻收益，按档位聚合统计均值/中位数/胜率。

数据约束（已实测确认，必须在结果中披露）：
  - 可复现：技术面（均线/排列/斜率/支撑压力/乖离/盘口）、基本面（财报同比，
    用 InfoPublDate 防未来函数）
  - 不可得：资金面（历史主力资金 / 两融接口仅提供当日快照，历史区间返回空）、
    消息面（历史资讯 / 研报情绪无法在回测时点无偏重构）
  → 回测版评分 = 技术面 + 基本面；资金面与消息面置零。

用法：
    python ai_score_backtest.py                 # 默认股票池 + 800 根日线
    python ai_score_backtest.py --codes sh601179,sh600000 --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "data" / "backtest_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 默认股票池：沪深主板 + 创业板，覆盖不同行业；含用户持仓关注标的
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

# 生产阈值（backend/analysis/rule_engine.py rule_based）
TH_ADD = 28.0      # >= 加仓
TH_HOLD = 5.0      # >= 观望
TH_REDUCE = -22.0  # >= 减仓，否则清仓

FORWARD_DAYS = (1, 3, 5, 10)
PRIMARY_DAYS = 5   # 主口径，对齐策略「5-10 个交易日」持有期

# warmup：MA60 需要 60 根，额外留 1.5 倍余量
WARMUP_BARS = 90


def westock_cli() -> list[str]:
    """返回 westock-data 调用命令：优先 PATH 命令，否则回退到 skill 脚本 + node。"""
    if os.getenv("WESTOCK_BIN"):
        return [os.environ["WESTOCK_BIN"]]
    skill = Path(
        r"C:\Users\王\.workbuddy\plugins\marketplaces\experts\plugins"
        r"\strategy-backtest-expert\skills\westock-data\scripts\index.js"
    )
    node = Path(r"C:\Users\王\.workbuddy\binaries\node\versions\22.22.2\node.exe")
    if skill.exists() and node.exists():
        return [str(node), str(skill)]
    return ["westock-data"]


def run_cli(args: list[str], timeout: int = 90) -> str:
    cmd = westock_cli() + args
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="ignore", timeout=timeout)
        return out.stdout or ""
    except Exception as exc:  # noqa: BLE001
        print(f"  [取数失败] {' '.join(args)} -> {exc}")
        return ""


def parse_md_table(text: str) -> pd.DataFrame:
    """解析 westock-data 的 Markdown 表格输出为 DataFrame。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 3:
        return pd.DataFrame()
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    rows: list[list[str]] = []
    for ln in lines[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) == len(header):
            rows.append(cells)
    return pd.DataFrame(rows, columns=header)


def to_num(v: Any) -> float | None:
    try:
        if v is None or v == "" or v == "null":
            return None
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 取数

def load_kline(code: str, limit: int) -> pd.DataFrame:
    """取前复权日线（带本地缓存，避免重复打接口）。"""
    cache = CACHE_DIR / f"kline_{code}_{limit}.csv"
    if cache.exists():
        df = pd.read_csv(cache)
        return df
    raw = run_cli(["kline", code, "--period", "day", "--limit", str(limit), "--fq", "qfq"])
    df = parse_md_table(raw)
    if df.empty:
        print(f"  [{code}] 日线为空")
        return df
    out = pd.DataFrame({
        "date": df["date"],
        "open": df["open"].map(to_num),
        "close": df["last"].map(to_num),      # westock 收盘列名为 last
        "high": df["high"].map(to_num),
        "low": df["low"].map(to_num),
        "volume": df["volume"].map(to_num),
        "turnover": df["exchange"].map(to_num),  # 换手率 %
    })
    # westock 返回倒序（最新在前），统一为升序
    out = out.iloc[::-1].reset_index(drop=True)
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    out.to_csv(cache, index=False)
    return out


def load_fundamentals(code: str) -> list[dict[str, Any]]:
    """取财报（利润表），返回按发布日升序的记录：用于防未来函数的同比计算。"""
    cache = CACHE_DIR / f"fin_{code}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    raw = run_cli(["finance", code, "--num", "12"])
    df = parse_md_table(raw)
    rows: list[dict[str, Any]] = []
    if not df.empty and "EndDate" in df.columns and "InfoPublDate" in df.columns:
        for _, r in df.iterrows():
            end = str(r.get("EndDate") or "")[:10]
            pub = str(r.get("InfoPublDate") or "")[:10]
            if not end or not pub:
                continue
            rows.append({
                "end_date": end,
                "pub_date": pub,
                "revenue": to_num(r.get("OperatingRevenue")),
                "net_profit": to_num(r.get("NPParentCompanyOwners")),
            })
    rows.sort(key=lambda x: x["pub_date"])
    cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


# ------------------------------------------------------------------ 技术面复现

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


def tech_score(d: pd.DataFrame, i: int) -> float:
    """第 i 根 bar 的技术面得分（严格只用 i 及之前的数据）。"""
    row = d.iloc[i]
    close = row["close"]
    if pd.isna(close):
        return 0.0

    score = 0.0
    # 均线站上条数
    above = 0
    for w in (5, 10, 20, 60):
        mav = row[f"ma{w}"]
        if pd.notna(mav) and close > mav:
            above += 1
    score += (above - 2) * 8

    # 排列
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

    # 斜率（与 5 日前比较，避免单日噪声）
    for w, weight in ((5, 3), (10, 3), (20, 4), (60, 4)):
        if i >= 5:
            prev = d.iloc[i - 5][f"ma{w}"]
            cur = row[f"ma{w}"]
            if pd.notna(prev) and pd.notna(cur):
                if cur > prev:
                    score += weight
                elif cur < prev:
                    score -= weight

    # 20 日涨跌
    if i >= 20 and pd.notna(d.iloc[i - 20]["close"]) and d.iloc[i - 20]["close"]:
        chg20 = (close - d.iloc[i - 20]["close"]) / d.iloc[i - 20]["close"] * 100
        score += 6 if chg20 > 0 else -6

    # 支撑压力：突破 20 日高 / 跌破 20 日低（用 i 之前的 20 日窗口）
    if i >= 21:
        prev_high = d.iloc[i - 20:i]["high"].max()
        prev_low = d.iloc[i - 20:i]["low"].min()
        if pd.notna(prev_high) and close > prev_high:
            score += 8
        elif pd.notna(prev_low) and close < prev_low:
            score -= 12

    # MA20 乖离
    if pd.notna(ma20) and ma20:
        dev = (close - ma20) / ma20 * 100
        if dev > 8:
            score -= 4
        elif dev < -8:
            score += 4

    # 盘口分项（日线近似收盘时点）
    score += intraday_score(d, i)
    return score


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
    turnover = row["turnover"] if pd.notna(row["turnover"]) else None
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


# ------------------------------------------------------------------ 基本面（防未来函数）

def fundamental_score(fins: list[dict[str, Any]], signal_date: str) -> float:
    """用「信号日已发布」的最新财报计算同比分（营收/净利各 ±2，clamp ±8）。"""
    known = [f for f in fins if f["pub_date"] <= signal_date]
    if not known:
        return 0.0
    latest = known[-1]
    end_date = latest["end_date"]
    # 去年同期（同为 12-31 / 06-30 / 03-31 口径）
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

def bucket_by_threshold(score: float) -> str:
    if score >= TH_ADD:
        return "加仓"
    if score >= TH_HOLD:
        return "观望"
    if score >= TH_REDUCE:
        return "减仓"
    return "清仓"


def bucket_by_quantile(score: float, q: dict[str, float]) -> str:
    """按分位数切档：Q4(最高) ~ Q1(最低)，用于检验评分本身的区分度。"""
    if score >= q["q75"]:
        return "Q4 高分"
    if score >= q["q50"]:
        return "Q3"
    if score >= q["q25"]:
        return "Q2"
    return "Q1 低分"


# ------------------------------------------------------------------ 主流程

async def build_events(codes: list[str], limit: int) -> pd.DataFrame:
    events: list[dict[str, Any]] = []
    for code in codes:
        df = load_kline(code, limit)
        if len(df) < WARMUP_BARS + 20:
            print(f"  [{code}] 数据不足，跳过")
            continue
        d = compute_tech(df)
        fins = load_fundamentals(code)
        n = len(d)
        for i in range(WARMUP_BARS, n):
            date = str(d.iloc[i]["date"])[:10]
            t_score = tech_score(d, i)
            f_score = fundamental_score(fins, date)
            total = round(t_score + f_score, 1)
            ev: dict[str, Any] = {
                "symbol": code,
                "signal_date": date,
                "tech_score": round(t_score, 1),
                "fundamental_score": round(f_score, 1),
                "score": total,
                "close": d.iloc[i]["close"],
            }
            # 前瞻收益：次日开盘买入，持有 N 日后收盘卖出
            if i + 1 >= n:
                continue
            buy_price = d.iloc[i + 1]["open"]
            if pd.isna(buy_price) or not buy_price:
                continue
            ev["buy_date"] = str(d.iloc[i + 1]["date"])[:10]
            ev["buy_price"] = round(float(buy_price), 3)
            ok = False
            for nd in FORWARD_DAYS:
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
        print(f"  [{code}] 完成，事件 {len(events)} 条")
    return pd.DataFrame(events)


def summarize(df: pd.DataFrame, col: str, order: list[str]) -> pd.DataFrame:
    """按档位聚合前瞻收益统计。"""
    rows = []
    for b in order:
        sub = df[df[col] == b]
        if sub.empty:
            continue
        row: dict[str, Any] = {"档位": b, "样本数": len(sub), "占比%": round(len(sub) / len(df) * 100, 1)}
        for nd in FORWARD_DAYS:
            vals = sub[f"fwd{nd}"].dropna()
            if vals.empty:
                continue
            row[f"{nd}日均值%"] = round(vals.mean(), 3)
            row[f"{nd}日中位%"] = round(vals.median(), 3)
            row[f"{nd}日胜率%"] = round((vals > 0).mean() * 100, 1)
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=",".join(DEFAULT_CODES))
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--prefix", default="ai_score")
    args = ap.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    print(f"标的池 {len(codes)} 只，日线 {args.limit} 根")
    df = asyncio.run(build_events(codes, args.limit))
    if df.empty:
        print("无有效事件")
        sys.exit(1)
    # 只保留前瞻窗口完整的事件：末尾几根 K 线没有未来 5/10 日数据，
    # 收益为空的行若参与统计会污染样本数与占比，统一在此剔除
    before = len(df)
    df = df[df[f"fwd{PRIMARY_DAYS}"].notna()].reset_index(drop=True)
    print(f"剔除前瞻窗口不完整的事件 {before - len(df)} 条，有效事件 {len(df)} 条")

    # 分位数切档阈值
    q = {
        "q25": float(df["score"].quantile(0.25)),
        "q50": float(df["score"].quantile(0.50)),
        "q75": float(df["score"].quantile(0.75)),
    }
    df["档位_阈值"] = df["score"].map(bucket_by_threshold)
    df["档位_分位"] = df["score"].map(lambda s: bucket_by_quantile(s, q))

    # 事件级输出（事件研究契约：一行一事件，pnl_pct 为核心字段）
    out = df.copy()
    out["pnl_pct"] = out[f"fwd{PRIMARY_DAYS}"]
    out["holding_days"] = PRIMARY_DAYS
    out["label"] = out["档位_阈值"] + "档（评分 " + out["score"].astype(str) + "）"
    out["entry_date"] = out["buy_date"]
    out["exit_date"] = out.get(f"sell{PRIMARY_DAYS}_date")
    out["entry_price"] = out["buy_price"]
    cols = ["symbol", "signal_date", "entry_date", "exit_date", "entry_price",
            "score", "tech_score", "fundamental_score", "档位_阈值", "档位_分位",
            "pnl_pct", "holding_days", "label"]
    out = out[cols]
    out.to_csv(f"{args.prefix}_trades.csv", index=False, encoding="utf-8-sig")

    # 分档统计
    s_threshold = summarize(df, "档位_阈值", ["加仓", "观望", "减仓", "清仓"])
    s_quantile = summarize(df, "档位_分位", ["Q1 低分", "Q2", "Q3", "Q4 高分"])
    s_threshold.to_csv(f"{args.prefix}_bucket_threshold.csv", index=False, encoding="utf-8-sig")
    s_quantile.to_csv(f"{args.prefix}_bucket_quantile.csv", index=False, encoding="utf-8-sig")

    # 事件级汇总（禁止伪造 Sharpe / 年化 / 最大回撤）
    vals = df[f"fwd{PRIMARY_DAYS}"].dropna()
    summary = {
        "meta": {
            "strategy_name": "AI 分析策略评分阈值有效性检验（事件研究）",
            "symbol": f"{len(codes)} 只 A 股面板",
            "start": str(df["signal_date"].min()),
            "end": str(df["signal_date"].max()),
            "market": "china_a",
            "note": "回测版评分=技术面+基本面；资金面与消息面历史数据不可得，置零",
        },
        "summary": {
            "total_events": int(len(df)),
            "avg_return_pct": round(float(vals.mean()), 3),
            "median_return_pct": round(float(vals.median()), 3),
            "win_rate_pct": round(float((vals > 0).mean() * 100), 1),
            "best_event_pct": round(float(vals.max()), 3),
            "worst_event_pct": round(float(vals.min()), 3),
            "quantile_cuts": {k: round(v, 2) for k, v in q.items()},
        },
        "bucket_threshold": s_threshold.to_dict(orient="records"),
        "bucket_quantile": s_quantile.to_dict(orient="records"),
    }
    with open(f"{args.prefix}_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n事件 {len(df)} 条，评分区间 [{df['score'].min()}, {df['score'].max()}]")
    print(f"分位切点: Q25={q['q25']:.1f} 中位={q['q50']:.1f} Q75={q['q75']:.1f}")
    print("\n=== 生产阈值分档 ===")
    print(s_threshold.to_string(index=False))
    print("\n=== 分位数分档 ===")
    print(s_quantile.to_string(index=False))


if __name__ == "__main__":
    main()
