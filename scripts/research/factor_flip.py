"""决定性检验：把动量因子方向取反，看区分度是否显著改善。

因子归因（factor_attrib.py）发现所有趋势跟随类因子 IC 均为负（20日涨跌 IC=-0.042，
均线排列 -0.025，斜率 -0.018），而唯一的反转因子（MA20 乖离修正）IC 最高（+0.066）
却只有 ±4 分权重。本脚本验证：不拟合任何参数、仅把方向翻转，区分度是否改善。

对比四种组合（均不做参数拟合，避免过拟合）：
  A 生产口径      ：动量正向 + 乖离修正 + 盘口
  B 动量取反      ：动量全部取反，其余不变
  C 反转主导      ：动量取反 + 乖离修正×3 + 盘口
  D 去均线三兄弟  ：只保留 chg20 / 乖离 / 盘口（缓解共线）
另加 E 样本内 IC 加权（会过拟合，仅供上界参考，必须标注）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 动态推导仓库根，避免硬编码（脚本位于 <repo>/scripts/research/*.py）
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from factor_attrib import (  # noqa: E402
    MOMENTUM, REVERSAL, welch_t, bucket_spread, build_events, CODES, PRIMARY,
)

NAMES = {
    "SC_A": "A 生产口径",
    "SC_B": "B 动量取反",
    "SC_C": "C 反转主导",
    "SC_D": "D 去均线三兄弟",
    "SC_E": "E IC加权(样本内)",
}


def yearly(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    rows = []
    for y in sorted(df["year"].unique()):
        yd = df[df["year"] == y]
        row = {"年份": f"{y}年", "样本": len(yd)}
        for c in cols:
            r = bucket_spread(yd, c, c)
            row[NAMES[c].split(" ")[0]] = r.get("多空差%")
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    print("=" * 78)
    print("决定性检验：动量方向翻转")
    print("=" * 78)
    df = build_events(CODES)
    df = df[df[f"fwd{PRIMARY}"].notna()].reset_index(drop=True)
    df["year"] = df["signal_date"].astype(str).str[:4]
    print(f"事件 {len(df)} 条，区间 {df['signal_date'].min()} ~ {df['signal_date'].max()}")

    mom = df[MOMENTUM].sum(axis=1)
    dev = df[REVERSAL].sum(axis=1)
    intra = df["F_intraday"]
    fund = df["F_fund"]

    df["SC_A"] = mom + dev + intra + fund
    df["SC_B"] = -mom + dev + intra + fund
    df["SC_C"] = -mom + 3 * dev + intra
    df["SC_D"] = -df["F_chg20"] + 3 * dev + intra
    # E：样本内 IC 加权（过拟合上界，仅参考）
    ics = {}
    for f in MOMENTUM + REVERSAL + ["F_intraday", "F_fund"]:
        ics[f] = float(df[f].rank().corr(df[f"fwd{PRIMARY}"].rank()))
    df["SC_E"] = sum(df[f] * ics[f] for f in ics)
    print("\n各因子 IC（用于 E 组加权，样本内会高估）:")
    for f, v in ics.items():
        print(f"  {f:12s} {v:+.4f}")

    cols = ["SC_A", "SC_B", "SC_C", "SC_D", "SC_E"]
    print("\n" + "=" * 78)
    print("【1】四分位多空区分度（前瞻 5 日，次日开盘买入）")
    print("=" * 78)
    rows = []
    for c in cols:
        r = bucket_spread(df, c, NAMES[c])
        rows.append(r)
    res = pd.DataFrame(rows)
    print(res.to_string(index=False))

    print("\n" + "=" * 78)
    print("【2】分年度 Q4-Q1 多空差（稳健性）")
    print("=" * 78)
    yr = yearly(df, cols)
    print(yr.to_string(index=False))

    print("\n" + "=" * 78)
    print("【3】B 组（动量取反）按新阈值切四档的实际表现")
    print("=" * 78)
    q = df["SC_B"].quantile([0.2, 0.4, 0.6, 0.8]).to_dict()
    def bkt(s: float) -> str:
        if s >= q[0.8]:
            return "Q5 最高"
        if s >= q[0.6]:
            return "Q4"
        if s >= q[0.4]:
            return "Q3"
        if s >= q[0.2]:
            return "Q2"
        return "Q1 最低"
    df["B档"] = df["SC_B"].map(bkt)
    order = ["Q1 最低", "Q2", "Q3", "Q4", "Q5 最高"]
    from backend.backtest import engine
    bt = engine.summarize_by_bucket(df, "B档", order)
    print(bt[["档位", "样本数", "占比%", "1日均值%", "3日均值%", "5日均值%", "10日均值%",
              "5日胜率%"]].to_string(index=False))

    # 单调性：五档均值是否随档位单调
    means = [float(bt[bt["档位"] == o][f"{PRIMARY}日均值%"].iloc[0]) for o in order
             if not bt[bt["档位"] == o].empty]
    print(f"\n五档 5 日均值序列: {[round(m, 3) for m in means]}")
    diffs = [means[i + 1] - means[i] for i in range(len(means) - 1)]
    mono = sum(1 for d in diffs if d > 0)
    print(f"相邻档递增次数 {mono}/{len(diffs)} → {'单调' if mono >= len(diffs) - 1 else '非单调'}")

    print("\n" + "=" * 78)
    print("【4】对照：A 组（生产口径）五档")
    print("=" * 78)
    qa = df["SC_A"].quantile([0.2, 0.4, 0.6, 0.8]).to_dict()
    def bkt_a(s: float) -> str:
        if s >= qa[0.8]:
            return "Q5 最高"
        if s >= qa[0.6]:
            return "Q4"
        if s >= qa[0.4]:
            return "Q3"
        if s >= qa[0.2]:
            return "Q2"
        return "Q1 最低"
    df["A档"] = df["SC_A"].map(bkt_a)
    bta = engine.summarize_by_bucket(df, "A档", order)
    print(bta[["档位", "样本数", "占比%", "5日均值%", "5日胜率%"]].to_string(index=False))
    means_a = [float(bta[bta["档位"] == o][f"{PRIMARY}日均值%"].iloc[0]) for o in order
               if not bta[bta["档位"] == o].empty]
    print(f"\n五档 5 日均值序列: {[round(m, 3) for m in means_a]}")
    diffs_a = [means_a[i + 1] - means_a[i] for i in range(len(means_a) - 1)]
    print(f"相邻档递增次数 {sum(1 for d in diffs_a if d > 0)}/{len(diffs_a)}")

    outdir = ROOT / "tmp" / "factor_attrib"
    outdir.mkdir(parents=True, exist_ok=True)
    res.to_csv(outdir / "flip_spread.csv", index=False, encoding="utf-8-sig")
    yr.to_csv(outdir / "flip_yearly.csv", index=False, encoding="utf-8-sig")
    df.to_csv(outdir / "flip_events.csv", index=False, encoding="utf-8-sig")
    print(f"\n产物目录: {outdir}")


if __name__ == "__main__":
    main()
