"""生成因子级归因报告：自包含 HTML 看板 + Markdown。

直接读取 factor_attrib.py 落盘的 factor_events.csv（含全部因子列与 fwd1/3/5/10），
无需重跑事件构造。补充计算：收益分布偏度、生产四档分布形态。
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
    MOMENTUM, REVERSAL, welch_t, bucket_spread, ALL_LABELS, PRIMARY,
)

SRC = OUT_DIR / "factor_events.csv"
OUT = OUT_DIR

FACTORS = list(ALL_LABELS.keys())


def period_of(date: str) -> str:
    y, m = int(str(date)[:4]), int(str(date)[5:7])
    return f"{y}H{1 if m <= 6 else 2}"


def dist_stats(vals: np.ndarray) -> dict:
    """收益分布形态：均值被少数极端值拉高时，散户实际体验由中位数/胜率决定。"""
    return {
        "mean": float(vals.mean()),
        "median": float(np.median(vals)),
        "win": float((vals > 0).mean() * 100),
        "skew": float(pd.Series(vals).skew()),
        "p25": float(np.percentile(vals, 25)),
        "p75": float(np.percentile(vals, 75)),
        "n": int(len(vals)),
    }


def main() -> None:
    df = pd.read_csv(SRC)
    df = df[df[f"fwd{PRIMARY}"].notna()].reset_index(drop=True)
    df["period"] = df["signal_date"].map(period_of)
    df["A"] = df[MOMENTUM + REVERSAL + ["F_intraday"]].sum(axis=1) + df["F_fund"]
    ycol = f"fwd{PRIMARY}"
    print(f"事件 {len(df)} 条，区间 {df['signal_date'].min()} ~ {df['signal_date'].max()}")

    # ---------- 1. 生产四档分布形态（右偏检验） ----------
    def bucket(s: float) -> str:
        if s >= 28:
            return "加仓"
        if s >= 5:
            return "观望"
        if s >= -22:
            return "减仓"
        return "清仓"
    df["档位"] = df["A"].map(bucket)
    order = ["加仓", "观望", "减仓", "清仓"]
    dist_rows = []
    for b in order:
        v = df[df["档位"] == b][ycol].to_numpy(dtype=float)
        s = dist_stats(v)
        dist_rows.append({"档位": b, "样本": s["n"], "占比%": round(s["n"] / len(df) * 100, 1),
                          "均值%": round(s["mean"], 3), "中位数%": round(s["median"], 3),
                          "胜率%": round(s["win"], 1), "偏度": round(s["skew"], 2),
                          "P25%": round(s["p25"], 2), "P75%": round(s["p75"], 2)})
    dist = pd.DataFrame(dist_rows)
    print("\n【生产四档收益分布形态】")
    print(dist.to_string(index=False))

    add = df[df["档位"] == "加仓"][ycol].to_numpy(dtype=float)
    clr = df[df["档位"] == "清仓"][ycol].to_numpy(dtype=float)
    print(f"\n加仓档 均值{add.mean():+.3f}% vs 中位数{np.median(add):+.3f}% "
          f"→ 均值高于中位数说明右偏（少数暴涨拉高均值）")
    print(f"清仓档 均值{clr.mean():+.3f}% vs 中位数{np.median(clr):+.3f}%")
    print(f"加仓档胜率 {(add > 0).mean() * 100:.1f}% vs 清仓档胜率 {(clr > 0).mean() * 100:.1f}%")

    # ---------- 2. IC 稳定性（核心证据） ----------
    periods = sorted(df["period"].unique())
    targets = FACTORS + ["A"]
    labels = {**ALL_LABELS, "A": "【生产总分】"}
    ic_rows = []
    for f in targets:
        vals = []
        for p in periods:
            sub = df[df["period"] == p]
            vals.append(float(sub[f].rank().corr(sub[ycol].rank())))
        pos = sum(1 for v in vals if v > 0)
        # NaN 保护
        mean_ic = float(np.nanmean(vals))
        std_ic = float(np.nanstd(vals, ddof=1))
        ic_rows.append({
            "因子": labels.get(f, f), "key": f,
            **{p: (round(v, 4) if v == v else None) for p, v in zip(periods, vals)},
            "均值IC": round(mean_ic, 4), "IC标准差": round(std_ic, 4),
            "为正期数": f"{pos}/{len(periods)}",
            "方向稳定": "是" if (pos >= len(periods) - 1 or pos <= 1) else "否",
            "IR": round(abs(mean_ic) / std_ic, 2) if std_ic and std_ic == std_ic else None,
        })
    ic = pd.DataFrame(ic_rows)
    print("\n【IC 半年期稳定性】")
    print(ic[["因子", "均值IC", "IC标准差", "为正期数", "方向稳定", "IR"]].to_string(index=False))

    # ---------- 3. 共线矩阵 ----------
    tech_f = ["F_above", "F_arrange", "F_slope", "F_chg20", "F_sr", "F_dev", "F_intraday"]
    corr = df[tech_f].corr(method="spearman").round(3)

    # ---------- 4. 逐因子有效性 ----------
    from factor_attrib import factor_report
    rep = factor_report(df, FACTORS)

    # ---------- 5. 四次证伪过程 ----------
    attempts = pd.DataFrame([
        {"轮次": "① 因子归因（全样本）", "做法": "逐因子算 IC，找最强/最弱",
         "结果": "乖离修正 IC+0.066 最高；20日涨跌 IC-0.042 最负", "是否可信": "仅描述，未检验"},
        {"轮次": "② 共线检验", "做法": "因子相关矩阵",
         "结果": "均线三兄弟平均相关 0.715，同一信号计 3 遍", "是否可信": "可信（无拟合）"},
        {"轮次": "③ 动量翻转（全样本）", "做法": "去共线 + chg20 反向 + 乖离×3",
         "结果": "多空差 +0.577%，t=4.05，四年全正", "是否可信": "❌ 前视，已证伪"},
        {"轮次": "④ 样本外（IC定方向）", "做法": "训练期 IC 定符号，测试期验证",
         "结果": "多空差 -0.458%，t=-2.25，两年全负", "是否可信": "✅ 证伪了③"},
        {"轮次": "⑤ 样本外（稳定性筛选）", "做法": "前3期 3/3 同号才入选，后4期验证",
         "结果": "多空差 -0.231%，t=-1.13，五档非单调", "是否可信": "✅ 最终结论"},
    ])

    # ---------- 6. 输出 Markdown ----------
    md = build_markdown(df, dist, ic, corr, rep, attempts, periods, add, clr)
    (OUT / "因子归因报告.md").write_text(md, encoding="utf-8")

    # ---------- 7. 输出 HTML ----------
    html = build_html(df, dist, ic, corr, rep, attempts, periods)
    (OUT / "因子归因报告.html").write_text(html, encoding="utf-8")

    dist.to_csv(OUT / "report_dist.csv", index=False, encoding="utf-8-sig")
    ic.to_csv(OUT / "report_ic.csv", index=False, encoding="utf-8-sig")
    print(f"\n产物: {OUT / '因子归因报告.html'}")
    print(f"      {OUT / '因子归因报告.md'}")


def build_markdown(df, dist, ic, corr, rep, attempts, periods, add, clr) -> str:
    a = dist[dist["档位"] == "加仓"].iloc[0]
    c = dist[dist["档位"] == "清仓"].iloc[0]
    return f"""# 首页 AI 分析策略 · 因子级归因报告

- **样本**：10 只 A 股 × 约 800 根前复权日线，{len(df)} 个事件
- **区间**：{df['signal_date'].min()} ~ {df['signal_date'].max()}
- **口径**：信号在当日收盘计算，次日开盘买入，持有 5 日后收盘卖出（与 `backend/backtest/score_strategy.py` 同口径）
- **数据限制**：资金面（主力资金/两融）与消息面（资讯/研报情绪）历史数据不可得，回测版评分 = 技术面 + 基本面

---

## 一句话结论

**这套评分不是"权重没调好"，而是因子集本身在 A 股 5 日尺度上没有可稳定提取的信号。**
更严重的是：生产总分的 IC 在 7 个半年期里**全部为负（0/7）**——评分越高，后续 5 日表现越差，方向高度一致。

> **对散户最致命的一点**：加仓档均值 {a['均值%']:+.3f}% 看起来是正的，但**中位数是 {a['中位数%']:+.3f}%**（偏度 {a['偏度']}）——
> 超过一半的"加仓"交易实际亏损，正收益完全由少数暴涨样本贡献。
> 反观清仓档中位数 {c['中位数%']:+.3f}%、胜率 {c['胜率%']}%，**实际体验反而更好**。

---

## 一、生产四档：均值与胜率背离

| 档位 | 样本 | 占比 | 均值 | 中位数 | 胜率 | 偏度 |
|---|---|---|---|---|---|---|
{dist.to_markdown(index=False)}

- 加仓档均值 {a['均值%']:+.3f}% 但胜率仅 {a['胜率%']}%，清仓档均值 {c['均值%']:+.3f}% 而胜率 {c['胜率%']}%
- 加仓档均值({a['均值%']:+.3f}%) 高于中位数({a['中位数%']:+.3f}%)，偏度 {a['偏度']} → **右偏**：少数暴涨样本把均值拉高，多数交易实际是亏的
- 对散户而言，胜率比均值重要：**均值正、胜率负的组合无法靠纪律执行**

## 二、逐因子有效性（前瞻 5 日）

{rep[['因子', '样本', 'IC', 'IC非重叠', '高分位5日%', '低分位5日%', '多空差%', 't', '判定']].to_markdown(index=False)}

## 三、共线：均线三兄弟是同一个信号计了三遍

{corr.to_markdown()}

- `F_arrange ~ F_slope = {corr.loc['F_arrange', 'F_slope']:.3f}`，三因子平均相关 **0.715**
- 三者权重合计 ±48 分，占技术面 ±78 的 **62%** → 同一个"价格趋势"信号被放大 3 倍，噪声同步放大

## 四、IC 半年期稳定性（核心证据）

{ic[['因子', '均值IC', 'IC标准差', '为正期数', '方向稳定', 'IR']].to_markdown(index=False)}

- **【生产总分】0/7 全负**：7 个半年期 IC 无一为正，均值 {ic[ic['key'] == 'A']['均值IC'].iloc[0]:+.4f}
- **唯一 7/7 稳定为正的是「MA20 乖离修正」**（均值 IC +0.0735，IR 1.541 最高），而它权重只有 ±4，占技术面 5%
- 所有趋势跟随类因子（斜率 0/7、20日涨跌 0/7、均线排列 1/7、突破 1/7）**稳定为负**
- `站上均线条数 3/7`、`盘口 4/7`、`基本面 2/7` → 符号反复翻转，是噪音不是信号

## 五、内部逻辑冲突

- 触发"超买扣分 -4"的 458 个事件中，**100%** 动量类因子合计为正（看多）
- 触发"超卖加分 +4"的 219 个事件中，**99.1%** 动量类因子合计为负（看空）
- 即：唯一方向正确的反转因子，在几乎 100% 的场景下都在对抗动量因子，且权重只有对方的 1/12

## 六、证伪过程（重要：我自己的"好结果"被推翻）

{attempts.to_markdown(index=False)}

第③轮拿到 t=4.05、四年全正的漂亮结果，但方向是用全样本 IC 定的（前视）。
第④⑤轮改用样本外后**全部失效甚至反向**。

**方法论教训**：在这套因子上，用 IC 均值定方向不可靠（基本面因子训练期 IC -0.059 vs 全样本 +0.017）。
任何基于全样本的"优化"都必须过样本外这一关。

## 七、对你交易偏好的验证

| 你的偏好 | 数据结论 |
|---|---|
| 禁用 5/10 日均线准则 | ✅ **数据支持**：斜率 IC 0/7 全负、均线排列 1/7、站上条数 3/7 不稳定 |
| 板块情绪周期 / 相对板块强弱 | ⚠️ 策略中**完全缺失**，且历史数据不可得，无法回测 |
| 主力净比 | ⚠️ 历史资金流数据不可得，本次**未纳入检验** |
| VWAP / 量比分层 | ⚠️ 量比子因子 IC 符号 4/7 翻转，当前口径无效 |
| 价格 + 时间双止损 | ❌ 当前"5-10 个交易日"仅文案，无强制时间止损 |

## 八、可执行建议

1. **不要用高分驱动加仓**。生产总分 IC 0/7 为负，加仓档胜率 {a['胜率%']}% 低于清仓档 {c['胜率%']}%
2. **唯一可保留的技术信号是 MA20 乖离修正**（7/7 稳定正 IC）：乖离 > +8% 减仓、< -8% 关注超跌反弹——这与你"板块高潮即减仓"的偏好一致
3. **趋势跟随类因子应整体降权或反向**，而非微调权重
4. **补齐资金面历史数据**是当前最大缺口：主力净比是你体系的核心，却完全无法验证
5. 信号可靠性表中 n=1/4 的条目（放量下挫、振幅剧烈、换手出货）应停止参与打分
"""


def bar_svg(rows, vmax=None, w=640, h=42) -> str:
    """极简横向条形图（红涨绿跌，符合 A 股习惯）。"""
    vmax = vmax or max(abs(r["v"]) for r in rows) or 1
    out = [f'<svg viewBox="0 0 {w} {h * len(rows) + 10}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    mid = w * 0.42
    for i, r in enumerate(rows):
        y = i * h + 8
        L = abs(r["v"]) / vmax * (w * 0.5)
        color = "#c0392b" if r["v"] >= 0 else "#1e8e5a"
        x = mid if r["v"] >= 0 else mid - L
        out.append(f'<text x="{mid - 8}" y="{y + 13}" text-anchor="end" font-size="12" fill="#333">{r["label"]}</text>')
        out.append(f'<rect x="{x}" y="{y + 3}" width="{max(L, 1)}" height="16" fill="{color}" rx="2"/>')
        out.append(f'<text x="{x + L + 6 if r["v"] >= 0 else x - 6}" y="{y + 16}" font-size="12" fill="{color}" '
                   f'text-anchor="{"start" if r["v"] >= 0 else "end"}">{r["v"]:+.3f}</text>')
    out.append(f'<line x1="{mid}" y1="0" x2="{mid}" y2="{h * len(rows)}" stroke="#bbb" stroke-width="1"/>')
    out.append("</svg>")
    return "".join(out)


def build_html(df, dist, ic, corr, rep, attempts, periods) -> str:
    def tbl(d: pd.DataFrame, cls="") -> str:
        cols = list(d.columns)
        th = "".join(f"<th>{c}</th>" for c in cols)
        trs = []
        for _, r in d.iterrows():
            tds = []
            for c in cols:
                v = r[c]
                if v is None or (isinstance(v, float) and v != v):
                    v = "—"
                cls2 = ""
                if c in ("均值IC", "IC", "多空差%", "t", "IR") and isinstance(v, (int, float)):
                    cls2 = "pos" if v > 0 else "neg" if v < 0 else ""
                if c == "方向稳定":
                    cls2 = "ok" if v == "是" else "bad"
                if c == "判定":
                    cls2 = "ok" if v in ("有效", "边际有效") else ("bad" if "矛盾" in str(v) else "")
                tds.append(f'<td class="{cls2}">{v}</td>')
            trs.append("<tr>" + "".join(tds) + "</tr>")
        return f'<table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>'

    a = dist[dist["档位"] == "加仓"].iloc[0]
    c = dist[dist["档位"] == "清仓"].iloc[0]

    ic_show = ic[["因子", "均值IC", "IC标准差", "为正期数", "方向稳定", "IR"]]
    ic_full = ic[[p for p in periods] + ["因子"]]

    bars = bar_svg([{"label": r["档位"], "v": r["均值%"]} for _, r in dist.iterrows()])

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AI 分析策略 · 因子级归因报告</title>
<style>
:root{{--bg:#f7f8fa;--card:#fff;--bd:#e3e6eb;--tx:#1f2329;--mut:#646a73;--pos:#c0392b;--neg:#1e8e5a}}
*{{box-sizing:border-box}}
body{{margin:0;padding:28px;background:var(--bg);color:var(--tx);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 6px}}
h2{{font-size:18px;margin:32px 0 12px;padding-left:10px;border-left:4px solid #4a6cf7}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:20px}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:18px 20px;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid var(--bd);padding:7px 10px;text-align:right;white-space:nowrap}}
th{{background:#f0f2f5;font-weight:600;text-align:right}}
td:first-child,th:first-child{{text-align:left}}
tbody tr:nth-child(even){{background:#fafbfc}}
.pos{{color:var(--pos);font-weight:600}}
.neg{{color:var(--neg);font-weight:600}}
.ok{{color:#1e8e5a;font-weight:600}}
.bad{{color:#c0392b;font-weight:600}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:18px}}
.k{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px}}
.k .v{{font-size:22px;font-weight:700;margin:2px 0}}
.k .l{{color:var(--mut);font-size:12px}}
.alert{{background:#fff4f0;border:1px solid #ffbb96;border-radius:8px;padding:14px 16px;margin:14px 0}}
.alert.ok{{background:#f0f9f4;border-color:#9ad3b4}}
ul{{margin:8px 0;padding-left:22px}}
li{{margin:4px 0}}
.scroll{{overflow-x:auto}}
code{{background:#f0f2f5;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body><div class="wrap">

<h1>首页 AI 分析策略 · 因子级归因报告</h1>
<div class="sub">样本 {len(df)} 个事件 · 10 只 A 股面板 · {df['signal_date'].min()} ~ {df['signal_date'].max()} ·
口径：信号日收盘计算 → 次日开盘买入 → 持有 5 日收盘卖出</div>

<div class="alert">
<b>一句话结论</b>：这套评分不是"权重没调好"，而是<b>因子集本身在 A 股 5 日尺度上没有可稳定提取的信号</b>。
更严重的是，生产总分的 IC 在 7 个半年期里<b>全部为负（0/7）</b>——评分越高，后续 5 日表现越差，方向高度一致。
</div>
<div class="alert">
<b>对散户最致命的一点</b>：加仓档均值 {a['均值%']:+.3f}% 看起来是正的，但<b>中位数是 {a['中位数%']:+.3f}%</b>（偏度 {a['偏度']}）——
超过一半的"加仓"交易实际亏损，正收益完全由少数暴涨样本贡献。
反观清仓档中位数 {c['中位数%']:+.3f}%、胜率 {c['胜率%']}%，<b>实际体验反而更好</b>。
</div>

<div class="kpi">
<div class="k"><div class="l">生产总分 IC</div><div class="v neg">{ic[ic['key']=='A']['均值IC'].iloc[0]:+.4f}</div><div class="l">7 个半年期 0/7 为正</div></div>
<div class="k"><div class="l">加仓档中位数收益</div><div class="v neg">{a['中位数%']:+.3f}%</div><div class="l">过半交易亏损 · 清仓档 {c['中位数%']:+.3f}%</div></div>
<div class="k"><div class="l">加仓档胜率</div><div class="v neg">{a['胜率%']}%</div><div class="l">低于清仓档 {c['胜率%']}%</div></div>
<div class="k"><div class="l">均线三兄弟平均相关</div><div class="v">0.715</div><div class="l">同一信号计入 3 遍</div></div>
<div class="k"><div class="l">唯一稳定正因子</div><div class="v">乖离修正</div><div class="l">7/7 期为正，权重仅 ±4</div></div>
</div>

<h2>一、生产四档：均值与胜率背离</h2>
<div class="card">
{tbl(dist)}
{bars}
<ul>
<li>加仓档均值 <b>{a['均值%']:+.3f}%</b> 但胜率仅 <b>{a['胜率%']}%</b>；清仓档均值 {c['均值%']:+.3f}% 而胜率 {c['胜率%']}%</li>
<li>加仓档均值高于中位数（{a['中位数%']:+.3f}%），偏度 {a['偏度']} → <b>右偏</b>：少数暴涨把均值拉高，多数交易实际亏损</li>
<li>对散户而言胜率比均值更重要：<b>均值正、胜率负的组合无法靠纪律执行</b></li>
</ul>
</div>

<h2>二、逐因子有效性（前瞻 5 日）</h2>
<div class="card scroll">
{tbl(rep[['因子','样本','IC','IC非重叠','高分位5日%','低分位5日%','多空差%','t','判定']])}
</div>

<h2>三、共线：均线三兄弟是同一个信号计了三遍</h2>
<div class="card scroll">
{tbl(corr.reset_index().rename(columns={'index':'因子'}))}
<p><code>F_arrange ~ F_slope = {corr.loc['F_arrange','F_slope']:.3f}</code>，三因子平均相关 <b>0.715</b>；
三者权重合计 ±48 分，占技术面 ±78 的 <b>62%</b> → 同一"价格趋势"信号被放大 3 倍，噪声同步放大。</p>
</div>

<h2>四、IC 半年期稳定性（核心证据）</h2>
<div class="card scroll">
{tbl(ic_show)}
<p style="margin-top:14px">各期明细：</p>
{tbl(ic_full)}
<ul>
<li><b>【生产总分】0/7 全负</b>：7 个半年期 IC 无一为正</li>
<li><b>唯一 7/7 稳定为正的是「MA20 乖离修正」</b>（均值 IC +0.0735，IR 1.541 最高），权重却只有 ±4，占技术面 5%</li>
<li>趋势跟随类因子（斜率 0/7、20日涨跌 0/7、均线排列 1/7、突破 1/7）<b>稳定为负</b></li>
<li>站上均线条数 3/7、盘口 4/7、基本面 2/7 → 符号反复翻转，是噪音</li>
</ul>
</div>

<h2>五、内部逻辑冲突</h2>
<div class="card">
<ul>
<li>触发"超买扣分 −4"的 458 个事件中，<b>100%</b> 动量类因子合计为正（看多）</li>
<li>触发"超卖加分 +4"的 219 个事件中，<b>99.1%</b> 动量类因子合计为负（看空）</li>
<li>即：唯一方向正确的反转因子，在几乎 100% 的场景下都在对抗动量因子，且权重只有对方的 1/12</li>
</ul>
</div>

<h2>六、证伪过程（我自己的"好结果"被推翻）</h2>
<div class="card scroll">
{tbl(attempts)}
<div class="alert">第③轮拿到 t=4.05、四年全正的漂亮结果，但方向是用全样本 IC 定的（前视）。
第④⑤轮改用样本外后<b>全部失效甚至反向</b>。<br><br>
<b>方法论教训</b>：在这套因子上用 IC 均值定方向不可靠（基本面因子训练期 IC −0.059 vs 全样本 +0.017）。
任何基于全样本的"优化"都必须过样本外这一关。</div>
</div>

<h2>七、对你交易偏好的验证</h2>
<div class="card scroll">
<table><thead><tr><th>你的偏好</th><th>数据结论</th></tr></thead><tbody>
<tr><td>禁用 5/10 日均线准则</td><td class="ok">✅ 数据支持：斜率 IC 0/7 全负、均线排列 1/7、站上条数 3/7 不稳定</td></tr>
<tr><td>板块情绪周期 / 相对板块强弱</td><td class="bad">⚠️ 策略中完全缺失，且历史数据不可得，无法回测</td></tr>
<tr><td>主力净比</td><td class="bad">⚠️ 历史资金流数据不可得，本次未纳入检验</td></tr>
<tr><td>VWAP / 量比分层</td><td class="bad">⚠️ 量比子因子 IC 符号 4/7 翻转，当前口径无效</td></tr>
<tr><td>价格 + 时间双止损</td><td class="bad">❌ 当前"5-10 个交易日"仅文案，无强制时间止损</td></tr>
</tbody></table>
</div>

<h2>八、可执行建议</h2>
<div class="card">
<ol>
<li><b>不要用高分驱动加仓</b>。生产总分 IC 0/7 为负，加仓档胜率 {a['胜率%']}% 低于清仓档 {c['胜率%']}%</li>
<li><b>唯一可保留的技术信号是 MA20 乖离修正</b>（7/7 稳定正 IC）：乖离 &gt; +8% 减仓、&lt; −8% 关注超跌反弹——与你"板块高潮即减仓"的偏好一致</li>
<li><b>趋势跟随类因子应整体降权或反向</b>，而非微调权重</li>
<li><b>补齐资金面历史数据是当前最大缺口</b>：主力净比是你体系的核心，却完全无法验证</li>
<li>信号可靠性表中 n=1/4 的条目（放量下挫、振幅剧烈、换手出货）应停止参与打分</li>
</ol>
</div>

<div class="sub" style="margin-top:26px">
数据限制：资金面（主力资金/两融）与消息面（资讯/研报情绪）历史数据不可得，
回测版评分 = 技术面 + 基本面；本结论检验的是技术面主导的评分体系。
事件研究不计手续费与滑点，不模拟仓位；日度事件前瞻窗口重叠，标准误被低估。
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
