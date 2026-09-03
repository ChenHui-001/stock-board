"""阈值重新标定：因子降权后，原 28/5/-22 已失效（技术面量级 ±78 → 约 ±40）。

方法（严格避免全样本过拟合）：
  训练期 2023-09 ~ 2024-12：网格搜索阈值，目标为四档分布合理 + 加仓档胜率不低于清仓档
  测试期 2025-01 起       ：只套用训练期选出的阈值，验证是否真的改善

评分口径与生产 `rule_engine.py` 完全一致（直接导入 FACTOR_WEIGHTS / _damp /
_round_half_away，不另写一份），仅基本面参与（资金面/消息面历史不可得，置零）。

对照三组：
  旧 = 原权重 + 原阈值 28/5/-22
  新 = 新权重 + 标定阈值
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 动态推导仓库根，避免硬编码（脚本位于 <repo>/scripts/research/*.py）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tmp"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from backend.analysis.rule_engine import FACTOR_WEIGHTS, _damp, _round_half_away  # noqa: E402
from factor_attrib import build_events, factor_breakdown, fundamental_score, CODES, PRIMARY  # noqa: E402

SPLIT = "2025-01-01"
OLD_TH = (28.0, 5.0, -22.0)


def intraday_v2(d: pd.DataFrame, i: int) -> int:
    """复现生产 _intraday_score（含 _damp 衰减），保证与生产同口径。"""
    row = d.iloc[i]
    price, prev = row["close"], row["prev_close"]
    hi, lo = row["high"], row["low"]
    chg = None
    if pd.notna(price) and pd.notna(prev) and prev:
        chg = (price - prev) / prev * 100
    if not (pd.notna(price) and pd.notna(prev) and pd.notna(hi)
            and pd.notna(lo) and hi > lo and chg is not None):
        return 0

    pos = (price - lo) / (hi - lo) * 100
    amp = (hi - lo) / prev * 100
    vr = row["volume"] / row["vol_ma5"] if (pd.notna(row["vol_ma5"]) and row["vol_ma5"]) else None
    turnover = row.get("turnover")
    turnover = turnover if (turnover is not None and pd.notna(turnover)) else None

    s = 0.0
    if pos >= 75:
        s += 3 * _damp("高位强势") if chg > 0 else -4 * _damp("冲高回落")
    elif pos <= 25:
        s += 2 * _damp("低位回升") if chg > 0 else -4 * _damp("低位下跌")
    if vr is not None:
        if vr >= 2:
            s += 3 * _damp("放量上攻") if chg > 0 else -3 * _damp("放量下挫")
        elif vr <= 0.6:
            s += -1 * _damp("缩量上涨") if chg > 0 else 1 * _damp("缩量下跌")
    if amp >= 8:
        s -= 2 * _damp("振幅剧烈")
    if turnover is not None:
        if turnover >= 10:
            s += -2 * _damp("换手出货") if chg < 0 else 1 * _damp("换手活跃")
        elif turnover <= 0.8:
            s -= 3 * _damp("交投清淡")
    return _round_half_away(max(-8.0, min(8.0, s)))


def score_v2(d: pd.DataFrame, i: int, fins: list, date: str) -> float:
    """新权重口径总分 = 技术面(加权) + 基本面。"""
    W = FACTOR_WEIGHTS
    fb = factor_breakdown(d, i)
    tech = (fb["F_above"] * W["above"] + fb["F_arrange"] * W["arrange"]
            + fb["F_slope"] * W["slope"] + fb["F_chg20"] * W["chg20"]
            + fb["F_sr"] * W["sr"] + fb["F_dev"] * W["dev"])
    tech += intraday_v2(d, i) * W["intraday"]
    return tech + fundamental_score(fins, date)


def score_old(d: pd.DataFrame, i: int, fins: list, date: str) -> float:
    """旧口径总分 = 技术面(原权重，盘口不衰减) + 基本面。"""
    fb = factor_breakdown(d, i)
    tech = (fb["F_above"] + fb["F_arrange"] + fb["F_slope"] + fb["F_chg20"]
            + fb["F_sr"] + fb["F_dev"] + fb["F_intraday"])
    return tech + fundamental_score(fins, date)


def build_scored(codes: list[str]) -> pd.DataFrame:
    """构造事件并同时算出新旧两种评分。"""
    from backend.backtest import engine
    rows = []
    for code in codes:
        df = engine.load_kline(code, 800)
        if df is None or len(df) < engine.WARMUP_BARS + 20:
            continue
        try:
            fins = engine.load_fundamentals(code)
        except Exception:  # noqa: BLE001
            fins = []
        from factor_attrib import compute_tech
        d = compute_tech(df)
        n = len(d)
        for i in range(engine.WARMUP_BARS, n):
            if i + 1 >= n:
                continue
            buy = d.iloc[i + 1]["open"]
            if pd.isna(buy) or not buy:
                continue
            date = str(d.iloc[i]["date"])[:10]
            ev = {"symbol": code, "signal_date": date,
                  "old": round(score_old(d, i, fins, date), 2),
                  "new": round(score_v2(d, i, fins, date), 2)}
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
                ev[f"fwd{nd}"] = round((sell / buy - 1) * 100, 3)
                ok = True
            if ok:
                rows.append(ev)
        print(f"  [{code}] 完成，累计 {len(rows)}")
    return pd.DataFrame(rows)


def bucket_eval(df: pd.DataFrame, col: str, th: tuple[float, float, float]) -> dict:
    """按阈值切四档，返回分布与表现。"""
    add, hold, red = th
    ycol = f"fwd{PRIMARY}"
    b = pd.cut(df[col], bins=[-np.inf, red, hold, add, np.inf],
               labels=["清仓", "减仓", "观望", "加仓"])
    t = df.assign(档=b)
    out = {"阈值": f"{add:.0f}/{hold:.0f}/{red:.0f}", "样本": len(t)}
    grp = {}
    for name in ["加仓", "观望", "减仓", "清仓"]:
        v = t[t["档"] == name][ycol].dropna().to_numpy(dtype=float)
        grp[name] = v
        out[f"{name}占比%"] = round(len(v) / len(t) * 100, 1) if len(t) else 0.0
        out[f"{name}胜率%"] = round(float((v > 0).mean() * 100), 1) if len(v) >= 30 else None
        out[f"{name}中位%"] = round(float(np.median(v)), 3) if len(v) >= 30 else None
    a, c = grp["加仓"], grp["清仓"]
    if len(a) >= 30 and len(c) >= 30:
        out["加仓-清仓胜率差"] = round(float((a > 0).mean() * 100 - (c > 0).mean() * 100), 1)
        out["加仓-清仓中位差"] = round(float(np.median(a) - np.median(c)), 3)
    else:
        out["加仓-清仓胜率差"] = None
        out["加仓-清仓中位差"] = None
    return out


def grid_search(train: pd.DataFrame) -> tuple[tuple[float, float, float], list[dict]]:
    """训练期网格搜索：分布合理 + 加仓档胜率尽量不低于清仓档。"""
    best, rows = None, []
    for add in range(0, 34, 2):
        for hold in range(-6, 16, 2):
            for red in range(-30, 4, 2):
                if not (add > hold > red):
                    continue
                r = bucket_eval(train, "new", (float(add), float(hold), float(red)))
                if r["加仓占比%"] < 12 or r["加仓占比%"] > 32:
                    continue
                if r["清仓占比%"] < 18 or r["清仓占比%"] > 40:
                    continue
                if r["观望占比%"] < 10 or r["减仓占比%"] < 10:
                    continue
                rows.append(r)
                if r["加仓-清仓胜率差"] is None:
                    continue
                # 目标：加仓-清仓胜率差为正且尽量大；中位差作为次级目标
                key = (r["加仓-清仓胜率差"], r["加仓-清仓中位差"])
                if best is None or key > best[0]:
                    best = (key, (float(add), float(hold), float(red)))
    return (best[1] if best else (12.0, 2.0, -10.0)), rows


def main() -> None:
    print("=" * 78)
    print("阈值重新标定（因子降权后）")
    print("=" * 78)
    df = build_scored(CODES)
    df = df[df[f"fwd{PRIMARY}"].notna()].reset_index(drop=True)
    train = df[df["signal_date"] < SPLIT]
    test = df[df["signal_date"] >= SPLIT]
    print(f"\n总事件 {len(df)}；训练期 {len(train)}；测试期 {len(test)}")

    print(f"\n新权重评分量级: 均值 {df['new'].mean():.2f}，标准差 {df['new'].std():.2f}，"
          f"范围 {df['new'].min():.1f} ~ {df['new'].max():.1f}")
    print(f"旧权重评分量级: 均值 {df['old'].mean():.2f}，标准差 {df['old'].std():.2f}，"
          f"范围 {df['old'].min():.1f} ~ {df['old'].max():.1f}")

    # ---------- 训练期标定 ----------
    print("\n" + "=" * 78)
    print("【1】训练期网格搜索（加仓档 12~32%、清仓档 18~40%）")
    print("=" * 78)
    th, cands = grid_search(train)
    print(f"候选组合 {len(cands)} 个")
    if cands:
        cdf = pd.DataFrame(cands).sort_values("加仓-清仓胜率差", ascending=False)
        print("\n训练期 Top10（按加仓-清仓胜率差）:")
        print(cdf.head(10).to_string(index=False))
    print(f"\n→ 选定阈值 加仓/观望/减仓 = {th[0]:.0f} / {th[1]:.0f} / {th[2]:.0f}")

    # ---------- 测试期验证 ----------
    print("\n" + "=" * 78)
    print("【2】测试期验证（阈值与权重均来自训练期，未参与拟合）")
    print("=" * 78)
    rows = [
        {"方案": "旧权重+旧阈值", **bucket_eval(test, "old", OLD_TH)},
        {"方案": "新权重+旧阈值", **bucket_eval(test, "new", OLD_TH)},
        {"方案": "新权重+新阈值", **bucket_eval(test, "new", th)},
    ]
    res = pd.DataFrame(rows)
    cols = ["方案", "阈值", "样本", "加仓占比%", "观望占比%", "减仓占比%", "清仓占比%",
            "加仓胜率%", "清仓胜率%", "加仓-清仓胜率差", "加仓中位%", "清仓中位%", "加仓-清仓中位差"]
    print(res[cols].to_string(index=False))

    # ---------- 训练期对照 ----------
    print("\n" + "=" * 78)
    print("【3】训练期对照（同口径，仅看拟合内表现）")
    print("=" * 78)
    rows_t = [
        {"方案": "旧权重+旧阈值", **bucket_eval(train, "old", OLD_TH)},
        {"方案": "新权重+新阈值", **bucket_eval(train, "new", th)},
    ]
    print(pd.DataFrame(rows_t)[cols].to_string(index=False))

    outdir = ROOT / "tmp" / "factor_attrib"
    outdir.mkdir(parents=True, exist_ok=True)
    res.to_csv(outdir / "calib_test.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(cands).to_csv(outdir / "calib_candidates.csv", index=False, encoding="utf-8-sig")
    df.to_csv(outdir / "calib_events.csv", index=False, encoding="utf-8-sig")
    print(f"\n产物目录: {outdir}")


if __name__ == "__main__":
    main()
