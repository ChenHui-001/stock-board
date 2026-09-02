"""因子级归因：把 rule_engine.tech_score 拆成独立子因子，逐项检验有效性。

与 8-31 的 score_strategy（验证「总分阈值」）不同，本脚本回答的是：
总分没有区分度，究竟是**没有有效因子**，还是**有效因子被噪音因子淹没**。

方法：
  1. 拆解 7 个技术面子因子 + 1 个基本面因子，逐因子算 fwd5 的分组收益/胜率/Spearman IC
  2. 因子相关矩阵 → 检验均线类因子共线（同一趋势信号被重复计入）
  3. 方向冲突检验 → 动量类（追涨）与反转类（乖离修正）是否互相抵消
  4. 剔除实验 → 全因子 vs 去均线 vs 仅有效因子，比较 Q4-Q1 多空价差

数据：复用 backend/backtest/engine 的缓存（10 只 A 股 × 800 根前复权日线 + 财报）。
口径：信号在 bar i 收盘产生，bar i+1 开盘买入，持有 N 日后收盘卖出（与 score_strategy 一致）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # scripts/factor_attribution/xxx.py -> 项目根
SCRIPT_DIR = Path(__file__).resolve().parent          # 本脚本所在目录
OUT_DIR = SCRIPT_DIR / "results"                      # 归因产物目录（随脚本走，换机器不失效）
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from backend.backtest import engine  # noqa: E402

CODES = [
    "sh601179", "sh600000", "sh600519", "sh600036", "sh601899",
    "sz000001", "sz000333", "sz000858", "sz002594", "sz300750",
]
LIMIT = 800
PRIMARY = 5

# 因子中文名（顺序 = 报告展示顺序）
FACTOR_LABELS = {
    "F_above": "站上均线条数 (above-2)×8",
    "F_arrange": "均线排列 ±8/±18",
    "F_slope": "四均线斜率 ±14",
    "F_chg20": "20日涨跌 ±6",
    "F_sr": "支撑压力突破/跌破 +8/-12",
    "F_dev": "MA20乖离修正 ±4",
    "F_intraday": "盘口信号 clamp ±8",
    "F_fund": "基本面 ±8",
}
# 子因子（把盘口再拆开）
SUB_LABELS = {
    "S_pos": "盘口·位置×涨跌",
    "S_vr": "盘口·量比",
    "S_amp": "盘口·振幅",
    "S_turn": "盘口·换手",
}
ALL_LABELS = {**FACTOR_LABELS, **SUB_LABELS}

# 方向归类：动量（追涨杀跌）vs 反转（高抛低吸）
MOMENTUM = ["F_above", "F_arrange", "F_slope", "F_chg20", "F_sr"]
REVERSAL = ["F_dev"]


def compute_tech(df: pd.DataFrame) -> pd.DataFrame:
    """与 score_strategy.compute_tech 完全同口径。"""
    d = df.copy()
    for w in (5, 10, 20, 60):
        d[f"ma{w}"] = d["close"].rolling(w).mean()
    d["prev_close"] = d["close"].shift(1)
    d["high20"] = d["high"].rolling(20).max()
    d["low20"] = d["low"].rolling(20).min()
    d["vol_ma5"] = d["volume"].shift(1).rolling(5).mean()
    return d


def intraday_subscores(d: pd.DataFrame, i: int) -> dict[str, float]:
    """复现 _intraday_score，但保留四个子项（不 clamp、不合并）。"""
    row = d.iloc[i]
    price, prev = row["close"], row["prev_close"]
    hi, lo = row["high"], row["low"]
    chg = None
    if pd.notna(price) and pd.notna(prev) and prev:
        chg = (price - prev) / prev * 100
    out = {"S_pos": 0.0, "S_vr": 0.0, "S_amp": 0.0, "S_turn": 0.0}
    if not (pd.notna(price) and pd.notna(prev) and pd.notna(hi)
            and pd.notna(lo) and hi > lo and chg is not None):
        return out

    pos = (price - lo) / (hi - lo) * 100
    amp = (hi - lo) / prev * 100
    vr = row["volume"] / row["vol_ma5"] if (pd.notna(row["vol_ma5"]) and row["vol_ma5"]) else None
    turnover = row.get("turnover")
    turnover = turnover if (turnover is not None and pd.notna(turnover)) else None

    # 位置 × 涨跌
    if pos >= 75:
        out["S_pos"] = 3.0 if chg > 0 else -4.0
    elif pos <= 25:
        out["S_pos"] = 2.0 if chg > 0 else -4.0
    # 量比
    if vr is not None:
        if vr >= 2:
            out["S_vr"] = 3.0 if chg > 0 else -3.0
        elif vr <= 0.6:
            out["S_vr"] = -1.0 if chg > 0 else 1.0
    # 振幅（收敛不计分）
    if amp >= 8:
        out["S_amp"] = -2.0
    # 换手
    if turnover is not None:
        if turnover >= 10:
            out["S_turn"] = 1.0 if chg > 0 else -2.0
        elif turnover <= 0.8:
            out["S_turn"] = -3.0
    return out


def factor_breakdown(d: pd.DataFrame, i: int) -> dict[str, float]:
    """第 i 根 bar 的因子分解（严格只用 i 及之前的数据）。"""
    row = d.iloc[i]
    close = row["close"]
    f = {k: 0.0 for k in ALL_LABELS}
    if pd.isna(close):
        return f

    ma5, ma10, ma20, ma60 = row["ma5"], row["ma10"], row["ma20"], row["ma60"]

    # F_above：站上几条均线
    above = sum(1 for m in (ma5, ma10, ma20, ma60) if pd.notna(m) and close > m)
    f["F_above"] = float((above - 2) * 8)

    # F_arrange：均线排列
    if all(pd.notna(x) for x in (ma5, ma10, ma20, ma60)):
        if ma5 > ma10 > ma20 > ma60:
            f["F_arrange"] = 18.0
        elif ma5 > ma10 > ma20:
            f["F_arrange"] = 8.0
        elif ma5 < ma10 < ma20 < ma60:
            f["F_arrange"] = -18.0
        elif ma5 < ma10 < ma20:
            f["F_arrange"] = -8.0

    # F_slope：四均线 5 日斜率
    if i >= 5:
        for w, weight in ((5, 3), (10, 3), (20, 4), (60, 4)):
            prev = d.iloc[i - 5][f"ma{w}"]
            cur = row[f"ma{w}"]
            if pd.notna(prev) and pd.notna(cur):
                if cur > prev:
                    f["F_slope"] += weight
                elif cur < prev:
                    f["F_slope"] -= weight

    # F_chg20：20 日涨跌
    if i >= 20 and pd.notna(d.iloc[i - 20]["close"]) and d.iloc[i - 20]["close"]:
        chg20 = (close - d.iloc[i - 20]["close"]) / d.iloc[i - 20]["close"] * 100
        f["F_chg20"] = 6.0 if chg20 > 0 else -6.0

    # F_sr：突破 / 跌破前 20 日区间
    if i >= 21:
        prev_high = d.iloc[i - 20:i]["high"].max()
        prev_low = d.iloc[i - 20:i]["low"].min()
        if pd.notna(prev_high) and close > prev_high:
            f["F_sr"] = 8.0
        elif pd.notna(prev_low) and close < prev_low:
            f["F_sr"] = -12.0

    # F_dev：MA20 乖离修正（反转逻辑）
    if pd.notna(ma20) and ma20:
        dev = (close - ma20) / ma20 * 100
        if dev > 8:
            f["F_dev"] = -4.0
        elif dev < -8:
            f["F_dev"] = 4.0

    # 盘口四项
    subs = intraday_subscores(d, i)
    for k, v in subs.items():
        f[k] = v
    f["F_intraday"] = max(-8.0, min(8.0, sum(subs.values())))
    return f


def fundamental_score(fins: list[dict], signal_date: str) -> float:
    """信号日已发布的最新财报同比分（与 score_strategy 同口径）。"""
    known = [x for x in fins if x["pub_date"] <= signal_date]
    if not known:
        return 0.0
    latest = known[-1]
    end_date = latest["end_date"]
    prev_year = str(int(end_date[:4]) - 1) + end_date[4:]
    prev = next((x for x in known if x["end_date"] == prev_year), None)
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
    return max(-8.0, min(8.0, score))


def build_events(codes: list[str]) -> pd.DataFrame:
    events: list[dict] = []
    for code in codes:
        try:
            df = engine.load_kline(code, LIMIT)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{code}] 日线失败: {exc}")
            continue
        if df is None or len(df) < engine.WARMUP_BARS + 20:
            print(f"  [{code}] 数据不足")
            continue
        try:
            fins = engine.load_fundamentals(code)
        except Exception:  # noqa: BLE001
            fins = []
        d = compute_tech(df)
        n = len(d)
        for i in range(engine.WARMUP_BARS, n):
            if i + 1 >= n:
                continue
            buy_price = d.iloc[i + 1]["open"]
            if pd.isna(buy_price) or not buy_price:
                continue
            date = str(d.iloc[i]["date"])[:10]
            fb = factor_breakdown(d, i)
            fb["F_fund"] = fundamental_score(fins, date)
            ev = {"symbol": code, "signal_date": date, "buy_date": str(d.iloc[i + 1]["date"])[:10], **fb}
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
                ok = True
            if ok:
                events.append(ev)
        print(f"  [{code}] 完成，累计 {len(events)} 条")
    return pd.DataFrame(events)


# ------------------------------------------------------------------ 统计工具

def welch_t(a: np.ndarray, b: np.ndarray) -> float:
    """Welch t 统计量（大样本下正态近似足够，事件重叠会高估，仅作相对强弱参考）。"""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return 0.0
    va, vb = a.var(ddof=1) / na, b.var(ddof=1) / nb
    se = np.sqrt(va + vb)
    return float((a.mean() - b.mean()) / se) if se else 0.0


def factor_report(df: pd.DataFrame, factors: list[str]) -> pd.DataFrame:
    """逐因子：样本量、取值数、IC、IC(非重叠)、方向一致性、正负组收益差 + t。"""
    rows = []
    ycol = f"fwd{PRIMARY}"
    for f in factors:
        sub = df[["symbol", f, ycol]].dropna()
        if len(sub) < 100:
            rows.append({"因子": ALL_LABELS.get(f, f), "key": f, "样本": len(sub),
                         "取值数": 0, "IC": None, "IC非重叠": None,
                         "高分位5日%": None, "低分位5日%": None, "多空差%": None,
                         "胜率高": None, "胜率低": None, "t": None, "判定": "样本不足"})
            continue
        x = sub[f].to_numpy(dtype=float)
        y = sub[ycol].to_numpy(dtype=float)

        # Spearman IC（全样本 pooled）
        ic = float(pd.Series(x).rank().corr(pd.Series(y).rank()))
        # 非重叠抽样：每 5 行取 1 行（缓解日度事件重叠导致的标准误低估）
        nth = sub.iloc[::PRIMARY]
        ic_nth = float(nth[f].rank().corr(nth[ycol].rank())) if len(nth) >= 50 else float("nan")

        # 按因子值排序后切高低分位（用分位数而非简单正负，避免取值分布失衡）
        try:
            q_hi = sub[f].quantile(0.8)
            q_lo = sub[f].quantile(0.2)
        except Exception:  # noqa: BLE001
            continue
        hi = sub[sub[f] >= q_hi][ycol].to_numpy(dtype=float)
        lo = sub[sub[f] <= q_lo][ycol].to_numpy(dtype=float)
        if len(hi) < 20 or len(lo) < 20:
            rows.append({"因子": ALL_LABELS.get(f, f), "key": f, "样本": len(sub),
                         "取值数": int(sub[f].nunique()), "IC": round(ic, 4),
                         "IC非重叠": round(ic_nth, 4) if ic_nth == ic_nth else None,
                         "高分位5日%": None, "低分位5日%": None, "多空差%": None,
                         "胜率高": None, "胜率低": None, "t": None, "判定": "分位样本不足"})
            continue
        d_mean = float(hi.mean() - lo.mean())
        t = welch_t(hi, lo)
        wr_hi = float((hi > 0).mean() * 100)
        wr_lo = float((lo > 0).mean() * 100)

        # 判定：IC 与多空差同向 + |t|>=2 才算有效；同向但弱 = 边际；反向 = 失效/需反转
        consistent = (ic > 0 and d_mean > 0) or (ic < 0 and d_mean < 0)
        if consistent and abs(t) >= 2 and abs(ic) >= 0.02:
            verdict = "有效"
        elif consistent and abs(t) >= 1.5:
            verdict = "边际有效"
        elif consistent:
            verdict = "弱/噪音"
        else:
            verdict = "方向矛盾" if abs(t) >= 1.5 else "噪音"
        rows.append({
            "因子": ALL_LABELS.get(f, f), "key": f, "样本": len(sub),
            "取值数": int(sub[f].nunique()),
            "IC": round(ic, 4),
            "IC非重叠": round(ic_nth, 4) if ic_nth == ic_nth else None,
            "高分位5日%": round(float(hi.mean()), 3),
            "低分位5日%": round(float(lo.mean()), 3),
            "多空差%": round(d_mean, 3),
            "胜率高": round(wr_hi, 1), "胜率低": round(wr_lo, 1),
            "t": round(t, 2), "判定": verdict,
        })
    return pd.DataFrame(rows)


def bucket_spread(df: pd.DataFrame, score_col: str, label: str) -> dict:
    """按 score_col 切四分位，返回 Q4-Q1 多空价差 + 胜率差（衡量区分度）。"""
    ycol = f"fwd{PRIMARY}"
    sub = df[[score_col, ycol]].dropna()
    try:
        q = sub[score_col].quantile([0.25, 0.5, 0.75]).to_dict()
    except Exception:  # noqa: BLE001
        return {"组合": label, "样本": len(sub)}
    q1 = sub[sub[score_col] <= q[0.25]][ycol].to_numpy(dtype=float)
    q4 = sub[sub[score_col] >= q[0.75]][ycol].to_numpy(dtype=float)
    if len(q1) < 30 or len(q4) < 30:
        return {"组合": label, "样本": len(sub), "备注": "分位样本不足"}
    spread = float(q4.mean() - q1.mean())
    return {
        "组合": label, "样本": len(sub),
        "Q1低分5日%": round(float(q1.mean()), 3),
        "Q4高分5日%": round(float(q4.mean()), 3),
        "多空差%": round(spread, 3),
        "Q1胜率%": round(float((q1 > 0).mean() * 100), 1),
        "Q4胜率%": round(float((q4 > 0).mean() * 100), 1),
        "胜率差pct": round(float((q4 > 0).mean() * 100 - (q1 > 0).mean() * 100), 1),
        "t": round(welch_t(q4, q1), 2),
    }


def main() -> None:
    print("=" * 78)
    print("因子级归因：rule_engine 技术面拆解检验")
    print("=" * 78)
    df = build_events(CODES)
    print(f"\n事件总数 {len(df)}，剔除前瞻窗口不完整后：", end=" ")
    df = df[df[f"fwd{PRIMARY}"].notna()].reset_index(drop=True)
    print(len(df))
    print(f"区间 {df['signal_date'].min()} ~ {df['signal_date'].max()}")

    outdir = OUT_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    df.to_csv(outdir / "factor_events.csv", index=False, encoding="utf-8-sig")

    # ---------- 1. 逐因子有效性 ----------
    factors = list(FACTOR_LABELS.keys()) + list(SUB_LABELS.keys())
    rep = factor_report(df, factors)
    print("\n" + "=" * 78)
    print("【1】逐因子有效性（前瞻 5 日收益，次日开盘买入）")
    print("=" * 78)
    show = rep[["因子", "样本", "取值数", "IC", "IC非重叠", "高分位5日%", "低分位5日%",
                "多空差%", "胜率高", "胜率低", "t", "判定"]]
    print(show.to_string(index=False))
    rep.to_csv(outdir / "factor_report.csv", index=False, encoding="utf-8-sig")

    # ---------- 2. 因子相关矩阵 ----------
    print("\n" + "=" * 78)
    print("【2】技术面因子相关矩阵（检验共线：同一信号是否被重复计入）")
    print("=" * 78)
    tech_f = ["F_above", "F_arrange", "F_slope", "F_chg20", "F_sr", "F_dev", "F_intraday"]
    corr = df[tech_f].corr(method="spearman").round(3)
    print(corr.to_string())
    corr.to_csv(outdir / "factor_corr.csv", encoding="utf-8-sig")

    # 均线类内部平均相关
    ma_f = ["F_above", "F_arrange", "F_slope"]
    ma_corr = df[ma_f].corr(method="spearman")
    vals = [ma_corr.loc[a, b] for i, a in enumerate(ma_f) for b in ma_f[i + 1:]]
    print(f"\n均线三兄弟(above/arrange/slope) 两两相关: "
          + ", ".join(f"{a}~{b}={ma_corr.loc[a, b]:.3f}"
                      for i, a in enumerate(ma_f) for b in ma_f[i + 1:]))
    print(f"平均相关 {np.mean(vals):.3f}  → 共线程度: "
          f"{'极高' if np.mean(vals) > 0.7 else '高' if np.mean(vals) > 0.5 else '中' if np.mean(vals) > 0.3 else '低'}")

    # ---------- 3. 方向冲突（动量 vs 反转） ----------
    print("\n" + "=" * 78)
    print("【3】内部逻辑冲突：动量类（追涨）vs 反转类（乖离修正）")
    print("=" * 78)
    mom = df[MOMENTUM].sum(axis=1)
    rev = df[REVERSAL].sum(axis=1)
    conflict = (np.sign(mom) * np.sign(rev) < 0) & (mom != 0) & (rev != 0)
    print(f"动量合计与乖离修正方向相反的事件: {int(conflict.sum())} / {len(df)} "
          f"= {conflict.mean() * 100:.1f}%")
    # 乖离扣分时（超买 dev>8）动量是否必然为正
    dev_neg = df[df["F_dev"] < 0]
    if len(dev_neg):
        print(f"\n触发「超买扣分 -4」的事件 {len(dev_neg)} 条，其中动量合计为正(看多)的占 "
              f"{(dev_neg[MOMENTUM].sum(axis=1) > 0).mean() * 100:.1f}%")
        print(f"  这些事件里 above>=3 的占 {(dev_neg['F_above'] >= 8).mean() * 100:.1f}%")
        print(f"  → 乖离扣分平均抵消动量得分 {-(dev_neg[MOMENTUM].sum(axis=1).mean()):.1f} 分中的 4 分")
    dev_pos = df[df["F_dev"] > 0]
    if len(dev_pos):
        print(f"\n触发「超卖加分 +4」的事件 {len(dev_pos)} 条，其中动量合计为负(看空)的占 "
              f"{(dev_pos[MOMENTUM].sum(axis=1) < 0).mean() * 100:.1f}%")

    # ---------- 4. 剔除实验 ----------
    print("\n" + "=" * 78)
    print("【4】剔除实验：不同因子组合的四分位多空区分度")
    print("=" * 78)
    df = df.copy()
    df["SC_full"] = df[MOMENTUM + REVERSAL + ["F_intraday"]].sum(axis=1) + df["F_fund"]
    df["SC_prod"] = df["SC_full"]  # 生产口径（回测可得部分）
    df["SC_no_ma"] = df[["F_chg20", "F_sr", "F_dev", "F_intraday", "F_fund"]].sum(axis=1)
    df["SC_mom_only"] = df[MOMENTUM].sum(axis=1)
    df["SC_no_dev"] = df[MOMENTUM + ["F_intraday", "F_fund"]].sum(axis=1)

    # 仅有效因子：取 IC 方向一致且 |t|>=1.5 的因子
    valid = rep[(rep["判定"].isin(["有效", "边际有效"])) & rep["key"].isin(MOMENTUM + REVERSAL + ["F_intraday", "F_fund"])]
    valid_keys = [k for k in valid["key"].tolist()]
    print(f"入选有效因子: {valid_keys if valid_keys else '（无）'}")
    if valid_keys:
        df["SC_valid"] = df[valid_keys].sum(axis=1)

    combos = ["SC_full", "SC_no_ma", "SC_mom_only", "SC_no_dev"]
    if valid_keys:
        combos.append("SC_valid")
    names = {
        "SC_full": "A 全因子（生产口径）",
        "SC_no_ma": "B 剔除均线三兄弟",
        "SC_mom_only": "C 仅动量因子",
        "SC_no_dev": "D 剔除乖离修正",
        "SC_valid": "E 仅有效因子",
    }
    spread_rows = [bucket_spread(df, c, names[c]) for c in combos]
    spread_df = pd.DataFrame(spread_rows)
    print("\n" + spread_df.to_string(index=False))
    spread_df.to_csv(outdir / "combo_spread.csv", index=False, encoding="utf-8-sig")

    # ---------- 5. 生产阈值下的四档（对照 8-31 结论） ----------
    print("\n" + "=" * 78)
    print("【5】生产阈值 28/5/-22 四档分布（复核 8-31 结论）")
    print("=" * 78)
    def bucket(s: float) -> str:
        if s >= 28:
            return "加仓"
        if s >= 5:
            return "观望"
        if s >= -22:
            return "减仓"
        return "清仓"
    df["档位"] = df["SC_full"].map(bucket)
    bt = engine.summarize_by_bucket(df.rename(columns={f"fwd{PRIMARY}": f"fwd{PRIMARY}"}), "档位",
                                    ["加仓", "观望", "减仓", "清仓"])
    print(bt[["档位", "样本数", "占比%", "5日均值%", "5日胜率%"]].to_string(index=False))
    bt.to_csv(outdir / "threshold_bucket.csv", index=False, encoding="utf-8-sig")

    # ---------- 6. 分年度稳健性（有效因子 vs 全因子） ----------
    print("\n" + "=" * 78)
    print("【6】分年度稳健性：Q4-Q1 多空差")
    print("=" * 78)
    df["year"] = df["signal_date"].astype(str).str[:4]
    yr_rows = []
    for y in sorted(df["year"].unique()):
        yd = df[df["year"] == y]
        row = {"年份": f"{y}年", "样本": len(yd)}
        for c in combos:
            r = bucket_spread(yd, c, c)
            row[names[c].split(" ")[0]] = r.get("多空差%")
        yr_rows.append(row)
    yr_df = pd.DataFrame(yr_rows)
    print(yr_df.to_string(index=False))
    yr_df.to_csv(outdir / "yearly_spread.csv", index=False, encoding="utf-8-sig")

    print(f"\n产物目录: {outdir}")


if __name__ == "__main__":
    main()
