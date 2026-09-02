"""IC 稳定性检验：解释为什么「样本内显著、样本外失效」。

factor_flip.py 的 D 组全样本 t=4.05，factor_oos.py 的 C 组样本外 t=-2.25。
两者矛盾的根因假设是：**这些因子的 IC 本身符号不稳定**，样本内显著只是某几段
行情的偶然，换一段就翻向。本脚本按半年分期逐因子算 IC，量化符号翻转频率。

若某因子 IC 在 6 个半年期里正负各半 → 它不是信号，是噪音，任何基于全样本 IC
的权重校准都是在拟合噪音。
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
    MOMENTUM, REVERSAL, build_events, CODES, PRIMARY, ALL_LABELS,
)

ALL_F = MOMENTUM + REVERSAL + ["F_intraday", "F_fund"]


def period_of(date: str) -> str:
    y, m = int(date[:4]), int(date[5:7])
    return f"{y}H{1 if m <= 6 else 2}"


def main() -> None:
    print("=" * 78)
    print("IC 稳定性检验（按半年分期）")
    print("=" * 78)
    df = build_events(CODES)
    df = df[df[f"fwd{PRIMARY}"].notna()].reset_index(drop=True)
    df["period"] = df["signal_date"].map(period_of)
    # 生产口径总分
    df["A_total"] = df[MOMENTUM + REVERSAL + ["F_intraday"]].sum(axis=1) + df["F_fund"]

    periods = sorted(df["period"].unique())
    targets = ALL_F + ["A_total"]
    labels = {**ALL_LABELS, "A_total": "【生产总分】"}

    print(f"\n{'因子':<26}" + "".join(f"{p:>9s}" for p in periods) + f"{'均值':>9s}{'翻正':>6s}{'稳':>5s}")
    print("-" * (26 + 9 * len(periods) + 20))

    rows = []
    for f in targets:
        ic_by_p = {}
        for p in periods:
            sub = df[df["period"] == p]
            ic_by_p[p] = float(sub[f].rank().corr(sub[f"fwd{PRIMARY}"].rank()))
        vals = [ic_by_p[p] for p in periods]
        pos = sum(1 for v in vals if v > 0)
        mean_ic = float(np.mean(vals))
        # 稳定判定：6 期里至少 5 期同号才算方向稳定
        stable = "是" if (pos >= len(periods) - 1 or pos <= 1) else "否"
        print(f"{labels.get(f, f):<26}" + "".join(f"{v:>+9.4f}" for v in vals)
              + f"{mean_ic:>+9.4f}{pos:>5d}/{len(periods)}{stable:>5s}")
        rows.append({"因子": labels.get(f, f), "key": f, "均值IC": round(mean_ic, 4),
                     "为正期数": f"{pos}/{len(periods)}", "方向稳定": stable,
                     "IC标准差": round(float(np.std(vals, ddof=1)), 4),
                     "最小": round(min(vals), 4), "最大": round(max(vals), 4),
                     **{p: round(ic_by_p[p], 4) for p in periods}})

    out = pd.DataFrame(rows)
    print("\n" + "=" * 78)
    print("汇总：IC 方向与离散度")
    print("=" * 78)
    print(out[["因子", "均值IC", "IC标准差", "最小", "最大", "为正期数", "方向稳定"]].to_string(index=False))

    stable_cnt = int((out["方向稳定"] == "是").sum())
    print(f"\n方向稳定的因子: {stable_cnt} / {len(out)}")
    if stable_cnt == 0:
        print("→ 没有任何因子的 IC 方向在半年尺度上稳定，"
              "任何基于全样本 IC 的权重校准都是在拟合噪音。")

    # 信噪比：|均值IC| / IC标准差（类似 IR）
    out["IR"] = (out["均值IC"].abs() / out["IC标准差"]).round(3)
    print("\n按 |均值IC|/IC标准差 排序（近似信息比率，越高越可用）:")
    print(out[["因子", "均值IC", "IC标准差", "IR"]].sort_values("IR", ascending=False).to_string(index=False))

    outdir = OUT_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    out.to_csv(outdir / "ic_stability.csv", index=False, encoding="utf-8-sig")
    print(f"\n产物目录: {outdir}")


if __name__ == "__main__":
    main()
