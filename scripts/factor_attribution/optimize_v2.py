"""优化版评分（V2）回测对比：消除报告已确证的缺陷。

设计原则（每条都来自 factor_attrib 的实证结论，不是拍脑袋）：
  1. 剔除 IC 符号翻转的噪音因子：站上均线条数（3/7 为正）
  2. 大幅降权稳定为负的趋势跟随因子：排列 1/7、斜率 0/7、20日涨跌 0/7、突破 1/7
     —— 注意是「降权」而非「反向」：样本外检验已证明反向也无效（t=-1.13），
        反向是另一种形式的过拟合，降权才是对不确定性诚实的处理
  3. 提升唯一 7/7 稳定为正的因子权重：MA20 乖离修正 ±4 → ±12
  4. 盘口信号（4/7）与基本面（2/7）降权
  5. 收敛置信度与档位分布，避免高置信度驱动加仓

验证目标（不是追求收益，而是消除缺陷）：
  - 四档分布是否均衡（原：观望仅 16.6%，两极分化）
  - 加仓档胜率是否仍低于清仓档（原：48.4% vs 50.7%，倒挂）
  - 加仓档中位数是否为负（原：-0.109%）
  - IC 是否不再系统性为负（原：0/7 全负）
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

from factor_attrib import (  # noqa: E402
    MOMENTUM, REVERSAL, welch_t, build_events, CODES, PRIMARY,
)

SRC = OUT_DIR / "factor_events.csv"

# V2 权重（相对原分值的缩放系数）
W = {
    "F_above": 0.0,     # 剔除（IC 3/7 不稳定）
    "F_arrange": 1 / 3,  # ±18 → ±6
    "F_slope": 5 / 14,   # ±14 → ±5
    "F_chg20": 2 / 6,    # ±6  → ±2
    "F_dev": 3.0,        # ±4  → ±12（唯一 7/7 稳定正因子）
    "F_intraday": 0.5,   # ±8  → ±4
    "F_fund": 0.5,       # ±8  → ±4
}
# F_sr 正负不对称，单独处理：+8 → +3，-12 → -4
W_SR_POS, W_SR_NEG = 3 / 8, 4 / 12


def period_of(date: str) -> str:
    y, m = int(str(date)[:4]), int(str(date)[5:7])
    return f"{y}H{1 if m <= 6 else 2}"


def score_v2(df: pd.DataFrame) -> pd.Series:
    s = pd.Series(0.0, index=df.index)
    for f, w in W.items():
        s = s + df[f] * w
    sr = df["F_sr"]
    s = s + np.where(sr > 0, sr * W_SR_POS, sr * W_SR_NEG)
    return s


def dist_row(name: str, vals: np.ndarray, total: int) -> dict:
    return {
        "档位": name, "样本": len(vals),
        "占比%": round(len(vals) / total * 100, 1),
        "均值%": round(float(vals.mean()), 3),
        "中位数%": round(float(np.median(vals)), 3),
        "胜率%": round(float((vals > 0).mean() * 100), 1),
        "偏度": round(float(pd.Series(vals).skew()), 2),
    }


def quantile_compare(df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    """按分位数切四档（消除量纲差异，纯粹看区分度与分布形态）。"""
    q = df[col].quantile([0.25, 0.5, 0.75]).to_dict()
    total = len(df)
    ycol = f"fwd{PRIMARY}"
    names = ["Q1 最低", "Q2", "Q3", "Q4 最高"]
    buckets = [
        df[df[col] <= q[0.25]][ycol].to_numpy(float),
        df[(df[col] > q[0.25]) & (df[col] <= q[0.5])][ycol].to_numpy(float),
        df[(df[col] > q[0.5]) & (df[col] <= q[0.75])][ycol].to_numpy(float),
        df[df[col] > q[0.75]][ycol].to_numpy(float),
    ]
    return pd.DataFrame([dist_row(f"{label} {n}", b, total) for n, b in zip(names, buckets)])


def ic_by_period(df: pd.DataFrame, col: str) -> tuple[list[float], float]:
    ycol = f"fwd{PRIMARY}"
    vals = []
    for p in sorted(df["period"].unique()):
        sub = df[df["period"] == p]
        vals.append(float(sub[col].rank().corr(sub[ycol].rank())))
    return vals, float(np.nanmean(vals))


def main() -> None:
    df = pd.read_csv(SRC)
    df = df[df[f"fwd{PRIMARY}"].notna()].reset_index(drop=True)
    df["period"] = df["signal_date"].map(period_of)
    df["V1"] = df[MOMENTUM + REVERSAL + ["F_intraday"]].sum(axis=1) + df["F_fund"]
    df["V2"] = score_v2(df)
    print(f"事件 {len(df)} 条  {df['signal_date'].min()} ~ {df['signal_date'].max()}")

    print("\nV1 分值范围 [%.1f, %.1f]  V2 分值范围 [%.1f, %.1f]"
          % (df["V1"].min(), df["V1"].max(), df["V2"].min(), df["V2"].max()))

    # ---------- 1. 分位数四档对比 ----------
    print("\n" + "=" * 78)
    print("【1】分位数四档（各 25%）：分布形态与中位数")
    print("=" * 78)
    c1 = quantile_compare(df, "V1", "V1")
    c2 = quantile_compare(df, "V2", "V2")
    cmp_df = pd.concat([c1, c2], ignore_index=True)
    print(cmp_df.to_string(index=False))

    print("\n关键对比（Q4 最高分 vs Q1 最低分）:")
    for col, name in (("V1", "V1 旧版"), ("V2", "V2 新版")):
        q = df[col].quantile([0.25, 0.75]).to_dict()
        hi = df[df[col] >= q[0.75]][f"fwd{PRIMARY}"].to_numpy(float)
        lo = df[df[col] <= q[0.25]][f"fwd{PRIMARY}"].to_numpy(float)
        print(f"  {name}: Q4 中位数 {np.median(hi):+.3f}% 胜率 {(hi > 0).mean() * 100:.1f}%  |  "
              f"Q1 中位数 {np.median(lo):+.3f}% 胜率 {(lo > 0).mean() * 100:.1f}%  |  "
              f"中位数差 {np.median(hi) - np.median(lo):+.3f}pct  "
              f"胜率差 {((hi > 0).mean() - (lo > 0).mean()) * 100:+.1f}pct  t={welch_t(hi, lo):+.2f}")

    # ---------- 2. 阈值定档 ----------
    print("\n" + "=" * 78)
    print("【2】V2 阈值候选（按分位数取点，目标：四档均衡 + 加仓档稀缺）")
    print("=" * 78)
    qs = df["V2"].quantile([0.2, 0.35, 0.5, 0.65, 0.8, 0.9]).to_dict()
    print("V2 分位数: " + ", ".join(f"P{int(k * 100)}={v:.1f}" for k, v in qs.items()))

    # 目标：加仓 15% / 观望 30% / 减仓 30% / 清仓 25%（提升中性档占比）
    cand = {
        "加仓": float(df["V2"].quantile(0.85)),
        "观望": float(df["V2"].quantile(0.55)),
        "减仓": float(df["V2"].quantile(0.25)),
    }
    print(f"\n候选阈值: 加仓≥{cand['加仓']:.1f}  观望≥{cand['观望']:.1f}  减仓≥{cand['减仓']:.1f}")
    # 取整便于配置与阅读
    th_add, th_hold, th_reduce = round(cand["加仓"]), round(cand["观望"]), round(cand["减仓"])
    print(f"取整后  : 加仓≥{th_add}  观望≥{th_hold}  减仓≥{th_reduce}")

    def bucket_v2(s: float) -> str:
        if s >= th_add:
            return "加仓"
        if s >= th_hold:
            return "观望"
        if s >= th_reduce:
            return "减仓"
        return "清仓"
    df["V2档"] = df["V2"].map(bucket_v2)

    def bucket_v1(s: float) -> str:
        if s >= 28:
            return "加仓"
        if s >= 5:
            return "观望"
        if s >= -22:
            return "减仓"
        return "清仓"
    df["V1档"] = df["V1"].map(bucket_v1)

    print("\n" + "=" * 78)
    print("【3】生产阈值四档：V1（28/5/-22） vs V2（新阈值）")
    print("=" * 78)
    total = len(df)
    ycol = f"fwd{PRIMARY}"
    rows = []
    for col in ("V1档", "V2档"):
        for b in ["加仓", "观望", "减仓", "清仓"]:
            v = df[df[col] == b][ycol].to_numpy(float)
            if len(v) < 20:
                continue
            rows.append({"版本": col, **dist_row(b, v, total)})
    th_df = pd.DataFrame(rows)
    print(th_df.to_string(index=False))

    print("\n倒挂检验（加仓档 vs 清仓档）:")
    for col in ("V1档", "V2档"):
        a = df[df[col] == "加仓"][ycol].to_numpy(float)
        c = df[df[col] == "清仓"][ycol].to_numpy(float)
        print(f"  {col}: 加仓 胜率{(a > 0).mean() * 100:.1f}% 中位数{np.median(a):+.3f}%  |  "
              f"清仓 胜率{(c > 0).mean() * 100:.1f}% 中位数{np.median(c):+.3f}%  |  "
              f"胜率差{((a > 0).mean() - (c > 0).mean()) * 100:+.1f}pct  "
              f"中位数差{np.median(a) - np.median(c):+.3f}pct")

    # ---------- 4. IC 对比 ----------
    print("\n" + "=" * 78)
    print("【4】IC 对比（全样本均值 + 各半年期）")
    print("=" * 78)
    periods = sorted(df["period"].unique())
    for col in ("V1", "V2"):
        vals, mean_ic = ic_by_period(df, col)
        pos = sum(1 for v in vals if v > 0)
        print(f"{col}: 均值 IC {mean_ic:+.4f}  为正期数 {pos}/{len(vals)}  " +
              "  ".join(f"{p}={v:+.4f}" for p, v in zip(periods, vals)))

    # ---------- 5. 输出建议 ----------
    print("\n" + "=" * 78)
    print("【5】结论与落地参数")
    print("=" * 78)
    v2_hi = df[df["V2档"] == "加仓"][ycol].to_numpy(float)
    v1_hi = df[df["V1档"] == "加仓"][ycol].to_numpy(float)
    print(f"加仓档占比: V1 {len(v1_hi) / total * 100:.1f}%  →  V2 {len(v2_hi) / total * 100:.1f}%")
    print(f"观望档占比: V1 {(df['V1档'] == '观望').mean() * 100:.1f}%  →  "
          f"V2 {(df['V2档'] == '观望').mean() * 100:.1f}%")
    print(f"\n建议写入 rule_engine.py 的参数:")
    print(f"  TH_ADD={th_add}  TH_HOLD={th_hold}  TH_REDUCE={th_reduce}")
    print(f"  因子权重: {W}")
    print(f"  F_sr: 正 ×{W_SR_POS:.3f}  负 ×{W_SR_NEG:.3f}")

    outdir = OUT_DIR
    cmp_df.to_csv(outdir / "opt_quantile.csv", index=False, encoding="utf-8-sig")
    th_df.to_csv(outdir / "opt_threshold.csv", index=False, encoding="utf-8-sig")
    df.to_csv(outdir / "opt_events.csv", index=False, encoding="utf-8-sig")
    print(f"\n产物: {outdir}")


if __name__ == "__main__":
    main()
