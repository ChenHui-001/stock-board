"""最终检验：用「方向一致性」而非「IC 大小」筛选因子，严格样本外验证。

前面的教训：
  - factor_flip 的 D 组全样本 t=4.05，但符号用全样本 IC 定 → 过拟合
  - factor_oos 的 C 组用训练期 IC 定符号，样本外 t=-2.25 → IC 本身不可靠
  - factor_stability 显示：只有 F_dev 是 7/7 正，其余稳定因子全部为负，
    而 F_above / F_intraday / F_fund 的 IC 符号在半年尺度上反复翻转（噪音）

本脚本改用**稳健性筛选**：
  训练期 = 2023H2 / 2024H1 / 2024H2（前 3 期）
  入选条件 = 3/3 期 IC 同号（方向稳定）→ 符号取该方向，等权合成
  测试期 = 2025H1 起（后 4 期），完全未参与筛选

对照：
  A  生产口径
  S  稳定性筛选组合（等权、无拟合连续参数）
  D1 仅 F_dev（唯一 7/7 正因子，训练期 3/3 正同样入选）
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

ALL_F = MOMENTUM + REVERSAL + ["F_intraday", "F_fund"]
TRAIN_P = ["2023H2", "2024H1", "2024H2"]


def period_of(date: str) -> str:
    y, m = int(date[:4]), int(date[5:7])
    return f"{y}H{1 if m <= 6 else 2}"


def main() -> None:
    print("=" * 78)
    print("最终检验：方向一致性筛选 + 严格样本外验证")
    print("=" * 78)
    df = build_events(CODES)
    df = df[df[f"fwd{PRIMARY}"].notna()].reset_index(drop=True)
    df["period"] = df["signal_date"].map(period_of)
    df["A"] = df[MOMENTUM + REVERSAL + ["F_intraday"]].sum(axis=1) + df["F_fund"]

    train = df[df["period"].isin(TRAIN_P)]
    test = df[~df["period"].isin(TRAIN_P)]
    print(f"训练期 {TRAIN_P}  样本 {len(train)}")
    print(f"测试期 其余      样本 {len(test)}  期间 {sorted(test['period'].unique())}")

    # ---- 训练期：3/3 同号才入选 ----
    print("\n训练期各期 IC 与入选判定:")
    print(f"{'因子':<14}" + "".join(f"{p:>10s}" for p in TRAIN_P) + f"{'同号':>8s}{'判定':>10s}")
    selected: list[str] = []
    sign: dict[str, float] = {}
    for f in ALL_F:
        ics = []
        for p in TRAIN_P:
            sub = train[train["period"] == p]
            ics.append(float(sub[f].rank().corr(sub[f"fwd{PRIMARY}"].rank())))
        pos = sum(1 for v in ics if v > 0)
        # NaN 期（如财报早期无数据）视为不可用，直接剔除，不能当成"同号"
        has_nan = any(v != v for v in ics)
        ok = (not has_nan) and (pos == 3 or pos == 0)
        verdict = "剔除(NaN期)" if has_nan else (
            f"入选(符号{'+' if pos == 3 else '-'})" if ok else f"剔除(仅{pos}/3同号)")
        if ok:
            selected.append(f)
            sign[f] = 1.0 if pos == 3 else -1.0
        print(f"{f:<14}" + "".join(f"{v:>+10.4f}" for v in ics) + f"{pos:>7d}/3{verdict:>10s}")

    print(f"\n入选因子: {[(f, int(sign[f])) for f in selected]}")
    df["S"] = sum(df[f] * sign[f] for f in selected)
    df["D1"] = df["F_dev"]
    # 列在切分之后才构造，必须重新切一次测试期
    test = df[~df["period"].isin(TRAIN_P)].copy()

    cols = {"A": "A 生产口径", "S": "B 稳定性筛选(等权)", "D1": "C 仅乖离修正"}
    print("\n" + "=" * 78)
    print("【1】测试期（2025H1 起）四分位多空区分度")
    print("=" * 78)
    res = pd.DataFrame([bucket_spread(test, c, n) for c, n in cols.items()])
    print(res.to_string(index=False))

    print("\n" + "=" * 78)
    print("【2】测试期分半年 Q4-Q1 多空差")
    print("=" * 78)
    rows = []
    for p in sorted(test["period"].unique()):
        pd_ = test[test["period"] == p]
        row = {"期间": p, "样本": len(pd_)}
        for c, n in cols.items():
            r = bucket_spread(pd_, c, c)
            row[n.split(" ")[0]] = r.get("多空差%")
        rows.append(row)
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n" + "=" * 78)
    print("【3】测试期分半年 Q4-Q1 胜率差（pct）")
    print("=" * 78)
    rows = []
    for p in sorted(test["period"].unique()):
        pd_ = test[test["period"] == p]
        row = {"期间": p, "样本": len(pd_)}
        for c, n in cols.items():
            r = bucket_spread(pd_, c, c)
            row[n.split(" ")[0]] = r.get("胜率差pct")
        rows.append(row)
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n" + "=" * 78)
    print("【4】S 组测试期五档（散户可落地：只做多，看能否靠档位回避下跌）")
    print("=" * 78)
    q = test["S"].quantile([0.2, 0.4, 0.6, 0.8]).to_dict()
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
    t["档"] = t["S"].map(bkt)
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
    print(f"测试期基线 5 日均值 {base:.3f}%  → Q5 超额 {means[-1] - base:+.3f}pct，"
          f"Q1 超额 {means[0] - base:+.3f}pct")

    # 单调性 Spearman：档位序号 vs 各档均值
    ranks = list(range(1, len(means) + 1))
    mono = float(pd.Series(ranks).corr(pd.Series(means), method="spearman"))
    print(f"档位-收益 Spearman 单调性: {mono:+.2f}")

    outdir = ROOT / "tmp" / "factor_attrib"
    outdir.mkdir(parents=True, exist_ok=True)
    res.to_csv(outdir / "final_spread.csv", index=False, encoding="utf-8-sig")
    bt.to_csv(outdir / "final_quintile.csv", index=False, encoding="utf-8-sig")
    print(f"\n产物目录: {outdir}")


if __name__ == "__main__":
    main()
