"""样本外检验：用训练期决定「因子方向 + 去共线」，在测试期验证。

factor_flip.py 的 D 组（去均线三兄弟 + chg20 反向 + 乖离放大）在**全样本**上拿到
t=4.05 / 四年全正的漂亮结果，但方向和倍数是用全样本 IC 定的，存在前视。
本脚本消除该嫌疑：

  训练期 2023-09 ~ 2024-12  → 计算各因子 IC、相关矩阵，决定符号与去共线后的因子集
  测试期 2025-01 ~ 2026-08  → 只套用训练期结论，报告 Q4-Q1 多空差 / 胜率差 / 分年度

对照三组：
  A     生产口径（现有 rule_engine 权重，不做任何改动）
  OOS_F 全因子按训练期 IC 符号等权（不去共线）
  OOS_C 去共线后按训练期 IC 符号等权（|corr| > 0.6 的因子每簇只留 |IC| 最大者）
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

SPLIT = "2025-01-01"
CORR_CUT = 0.6
ALL_F = MOMENTUM + REVERSAL + ["F_intraday", "F_fund"]


def select_factors(train: pd.DataFrame) -> tuple[list[str], dict[str, float]]:
    """训练期：算 IC → 按 |IC| 降序贪心去共线（|corr|>0.6 剔除同簇弱者）。"""
    ics = {f: float(train[f].rank().corr(train[f"fwd{PRIMARY}"].rank())) for f in ALL_F}
    corr = train[ALL_F].corr(method="spearman")
    ranked = sorted(ALL_F, key=lambda f: abs(ics[f]), reverse=True)
    kept: list[str] = []
    for f in ranked:
        if all(abs(corr.loc[f, g]) <= CORR_CUT for g in kept):
            kept.append(f)
    return kept, ics


def report(df: pd.DataFrame, cols: dict[str, str]) -> pd.DataFrame:
    rows = []
    for c, name in cols.items():
        r = bucket_spread(df, c, name)
        rows.append(r)
    return pd.DataFrame(rows)


def main() -> None:
    print("=" * 78)
    print(f"样本外检验（训练期 IC 定方向与因子集，测试期验证）  分界 {SPLIT}")
    print("=" * 78)
    df = build_events(CODES)
    df = df[df[f"fwd{PRIMARY}"].notna()].reset_index(drop=True)
    df["year"] = df["signal_date"].astype(str).str[:4]

    train = df[df["signal_date"] < SPLIT].copy()
    test = df[df["signal_date"] >= SPLIT].copy()
    print(f"训练期 {len(train)} 条 ({train['signal_date'].min()} ~ {train['signal_date'].max()})")
    print(f"测试期 {len(test)} 条 ({test['signal_date'].min()} ~ {test['signal_date'].max()})")

    kept, ics = select_factors(train)
    dropped = [f for f in ALL_F if f not in kept]
    print("\n训练期各因子 IC:")
    for f in sorted(ALL_F, key=lambda x: -abs(ics[x])):
        mark = "保留" if f in kept else "剔除(共线)"
        print(f"  {f:12s} IC={ics[f]:+.4f}   {mark}")
    print(f"\n去共线后保留: {kept}")
    print(f"因共线剔除  : {dropped}")

    # 构造三个组合（符号与因子集全部来自训练期）
    sign = {f: (1.0 if ics[f] >= 0 else -1.0) for f in ALL_F}
    df["A"] = df[MOMENTUM + REVERSAL + ["F_intraday"]].sum(axis=1) + df["F_fund"]
    df["OOS_F"] = sum(df[f] * sign[f] for f in ALL_F)
    df["OOS_C"] = sum(df[f] * sign[f] for f in kept)
    # 列是在切分之后才构造的，必须重新切一次测试期
    test = df[df["signal_date"] >= SPLIT].copy()

    cols = {"A": "A 生产口径", "OOS_F": "B 全因子·训练期定符号", "OOS_C": "C 去共线·训练期定符号"}

    print("\n" + "=" * 78)
    print("【1】测试期（2025-01 起）四分位多空区分度")
    print("=" * 78)
    res = report(test, cols)
    print(res.to_string(index=False))

    print("\n" + "=" * 78)
    print("【2】测试期分年度 Q4-Q1 多空差")
    print("=" * 78)
    rows = []
    for y in sorted(test["year"].unique()):
        yd = test[test["year"] == y]
        row = {"年份": f"{y}年", "样本": len(yd)}
        for c, name in cols.items():
            r = bucket_spread(yd, c, c)
            row[name.split(" ")[0]] = r.get("多空差%")
        rows.append(row)
    yr = pd.DataFrame(rows)
    print(yr.to_string(index=False))

    print("\n" + "=" * 78)
    print("【3】测试期分年度 Q4-Q1 胜率差（pct）")
    print("=" * 78)
    rows = []
    for y in sorted(test["year"].unique()):
        yd = test[test["year"] == y]
        row = {"年份": f"{y}年", "样本": len(yd)}
        for c, name in cols.items():
            r = bucket_spread(yd, c, c)
            row[name.split(" ")[0]] = r.get("胜率差pct")
        rows.append(row)
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n" + "=" * 78)
    print("【4】C 组在测试期的五档单调性（散户可落地：只做多不做空）")
    print("=" * 78)
    q = test["OOS_C"].quantile([0.2, 0.4, 0.6, 0.8]).to_dict()
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
    t = test.copy()
    t["档"] = t["OOS_C"].map(bkt)
    order = ["Q1 最低", "Q2", "Q3", "Q4", "Q5 最高"]
    from backend.backtest import engine
    bt = engine.summarize_by_bucket(t, "档", order)
    print(bt[["档位", "样本数", "占比%", "1日均值%", "3日均值%", "5日均值%", "10日均值%",
              "5日胜率%"]].to_string(index=False))
    means = [float(bt[bt["档位"] == o][f"{PRIMARY}日均值%"].iloc[0]) for o in order
             if not bt[bt["档位"] == o].empty]
    print(f"\n五档 5 日均值: {[round(m, 3) for m in means]}")
    diffs = [means[i + 1] - means[i] for i in range(len(means) - 1)]
    print(f"相邻递增 {sum(1 for d in diffs if d > 0)}/{len(diffs)}")
    base = float(test[f"fwd{PRIMARY}"].mean())
    print(f"测试期全样本 5 日基线均值 {base:.3f}%")
    print(f"→ Q5 超额 {means[-1] - base:+.3f}pct / Q1 超额 {means[0] - base:+.3f}pct")

    outdir = ROOT / "tmp" / "factor_attrib"
    outdir.mkdir(parents=True, exist_ok=True)
    res.to_csv(outdir / "oos_spread.csv", index=False, encoding="utf-8-sig")
    yr.to_csv(outdir / "oos_yearly.csv", index=False, encoding="utf-8-sig")
    bt.to_csv(outdir / "oos_quintile.csv", index=False, encoding="utf-8-sig")
    pd.Series(ics, name="IC_train").to_csv(outdir / "oos_ic_train.csv", encoding="utf-8-sig")
    print(f"\n产物目录: {outdir}")


if __name__ == "__main__":
    main()
