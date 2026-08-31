"""渲染「AI 评分阈值有效性检验」仪表盘（事件研究 · 中文）。

读取 ai_score_* 标准产物，组装中文仪表盘到 index.html。
事件研究形态：不引用夏普/年化/最大回撤，只用事件级指标
（事件数、平均收益、中位数、胜率、最佳/最差事件）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from _render_dashboard import build_dashboard_data, render_dashboard

ROOT = Path(__file__).resolve().parent
TRADES = ROOT / "ai_score_trades.csv"
SUMMARY = ROOT / "ai_score_summary.json"
BUCKET_T = ROOT / "ai_score_bucket_threshold.csv"
BUCKET_Q = ROOT / "ai_score_bucket_quantile.csv"
TEMPLATE = ROOT / "_dashboard_template.html"

ORDER = ["加仓", "观望", "减仓", "清仓"]
QORDER = ["Q1 低分", "Q2", "Q3", "Q4 高分"]


def svg_bar_chart(rows: list[dict], title: str) -> str:
    """分档 5 日收益柱状图（纯 SVG，无外部依赖）。"""
    vals = [r["5日均值%"] for r in rows]
    names = [r["档位"] for r in rows]
    wins = [r["5日胜率%"] for r in rows]
    lo, hi = min(min(vals), -0.05), max(max(vals), 0.05)
    span = hi - lo
    W, H = 760, 260
    pad_l, pad_b, pad_t = 70, 46, 24
    plot_w = W - pad_l - 30
    plot_h = H - pad_b - pad_t
    zero_y = pad_t + plot_h * (hi / span) if span else pad_t + plot_h / 2

    bars, labels, winlines = [], [], []
    n = len(rows)
    slot = plot_w / n
    bw = slot * 0.5
    for i, (v, name, w) in enumerate(zip(vals, names, wins)):
        cx = pad_l + slot * (i + 0.5)
        y = pad_t + plot_h * ((hi - v) / span) if span else pad_t + plot_h / 2
        h = abs(y - zero_y)
        top = min(y, zero_y)
        color = "#e5534b" if v >= 0 else "#2ea86b"  # A 股：涨红跌绿
        bars.append(
            f'<rect x="{cx - bw/2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
            f'height="{h:.1f}" fill="{color}" rx="3"/>'
        )
        bars.append(
            f'<text x="{cx:.1f}" y="{(top - 6) if v >= 0 else (top + h + 14):.1f}" '
            f'text-anchor="middle" font-size="12" fill="{color}">{v:+.3f}%</text>'
        )
        labels.append(
            f'<text x="{cx:.1f}" y="{H - pad_b + 18:.1f}" text-anchor="middle" '
            f'font-size="13" fill="#c8ccd4">{name}</text>'
        )
        labels.append(
            f'<text x="{cx:.1f}" y="{H - pad_b + 34:.1f}" text-anchor="middle" '
            f'font-size="11" fill="#8b93a1">胜率 {w:.1f}%</text>'
        )
    winlines.append(
        f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{W - 30}" y2="{zero_y:.1f}" '
        f'stroke="#5a6270" stroke-width="1"/>'
    )
    winlines.append(
        f'<text x="{pad_l - 8}" y="{zero_y + 4:.1f}" text-anchor="end" '
        f'font-size="11" fill="#8b93a1">0%</text>'
    )
    return f"""<div class="bt-custom-bar-wrap">
  <div class="bt-custom-bar-title">{title}</div>
  <svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="{title}">
    {''.join(winlines)}
    {''.join(bars)}
    {''.join(labels)}
  </svg>
  <div class="bt-custom-bar-note">柱高 = 该档位信号后 5 个交易日的平均收益；下方标注该档胜率。
  若阈值有效，加仓档应显著高于清仓档。</div>
</div>"""


def build() -> None:
    trades = pd.read_csv(TRADES)
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    bt = pd.read_csv(BUCKET_T)
    bq = pd.read_csv(BUCKET_Q)

    # 分层抽样：每档取 120 行，控制 HTML 体积，同时保证每档可见
    sampled = trades.groupby("档位_阈值").head(120).reset_index(drop=True)
    sampled = sampled.sort_values("signal_date").reset_index(drop=True)
    # 事件数远超图表可承载量：全部行不画 marker，仅以表格呈现
    th = []
    for r in sampled.to_dict(orient="records"):
        row = dict(r)
        row["show_marker"] = False
        th.append(row)

    # ---- 分档指标表（生产阈值）
    rows_t = []
    for b in ORDER:
        sub = bt[bt["档位"] == b]
        if sub.empty:
            continue
        r = sub.iloc[0]
        rows_t.append({
            "metric": b,
            "values": [
                {"main": f"{int(r['样本数'])}"},
                {"main": f"{r['占比%']}%"},
                {"main": f"{r['5日均值%']:+.3f}%", "raw": r["5日均值%"]},
                {"main": f"{r['5日中位%']:+.3f}%", "raw": r["5日中位%"]},
                {"main": f"{r['5日胜率%']:.1f}%", "raw": r["5日胜率%"] - 50},
                {"main": f"{r['10日胜率%']:.1f}%", "raw": r["10日胜率%"] - 50},
            ],
        })

    # ---- 分位数分档指标表
    rows_q = []
    for b in QORDER:
        sub = bq[bq["档位"] == b]
        if sub.empty:
            continue
        r = sub.iloc[0]
        rows_q.append({
            "metric": b,
            "values": [
                {"main": f"{int(r['样本数'])}"},
                {"main": f"{r['5日均值%']:+.3f}%", "raw": r["5日均值%"]},
                {"main": f"{r['5日中位%']:+.3f}%", "raw": r["5日中位%"]},
                {"main": f"{r['5日胜率%']:.1f}%", "raw": r["5日胜率%"] - 50},
            ],
        })

    # ---- 分年度稳健性（加仓 vs 清仓 胜率差）
    trades["signal_date"] = pd.to_datetime(trades["signal_date"])
    trades["year"] = trades["signal_date"].dt.year
    rows_y = []
    for y in sorted(trades["year"].unique()):
        yd = trades[trades["year"] == y]
        a = yd[yd["档位_阈值"] == "加仓"]["pnl_pct"]
        c = yd[yd["档位_阈值"] == "清仓"]["pnl_pct"]
        if len(a) < 20 or len(c) < 20:
            rows_y.append({
                "metric": f"{y} 年",
                "values": [{"main": f"{len(a)}"}, {"main": f"{len(c)}"},
                           {"main": "样本不足"}],
            })
            continue
        diff = (a > 0).mean() * 100 - (c > 0).mean() * 100
        rows_y.append({
            "metric": f"{y} 年",
            "values": [
                {"main": f"{len(a)}"},
                {"main": f"{len(c)}"},
                {"main": f"{(a > 0).mean() * 100:.1f}%"},
                {"main": f"{(c > 0).mean() * 100:.1f}%"},
                {"main": f"{diff:+.1f}pct", "raw": diff},
            ],
        })

    s = summary["summary"]
    extra = [
        {
            "type": "text",
            "tab": "overview",
            "title": "结论",
            "text": (
                "- **生产阈值 28 / 5 / -22 不成立**：加仓档信号后 5 日平均收益 "
                f"{bt[bt['档位'] == '加仓'].iloc[0]['5日均值%']:+.3f}%、胜率 "
                f"{bt[bt['档位'] == '加仓'].iloc[0]['5日胜率%']:.1f}%；清仓档 "
                f"{bt[bt['档位'] == '清仓'].iloc[0]['5日均值%']:+.3f}%、胜率 "
                f"{bt[bt['档位'] == '清仓'].iloc[0]['5日胜率%']:.1f}%。两档收益几乎相同，"
                "且加仓档胜率反而更低。\n"
                "- **评分本身也缺乏区分度**：按分数四分位切档后仍无单调性，"
                "最高分组（Q4）5 日胜率 48.0%，反而低于 Q2 的 51.2%。\n"
                "- **2024 年出现显著反向**：加仓档胜率比清仓档低 12.8 个百分点（样本充足）。\n"
                "- **四档分布两极分化**：清仓 34.0% + 加仓 28.8% 占六成以上，"
                "真正中性的「观望」仅 16.6%。"
            ),
        },
        {
            "type": "custom_html",
            "tab": "overview",
            "title": "各档位信号后 5 日收益对比",
            "width": "full",
            "html": svg_bar_chart(bt.to_dict(orient="records"),
                                  "生产阈值分档：信号后 5 日平均收益"),
        },
        {
            "type": "metric_table",
            "tab": "overview",
            "title": "生产阈值分档统计（信号后 5/10 日）",
            "columns": ["档位", "样本数", "占比", "5日均值", "5日中位", "5日胜率", "10日胜率"],
            "rows": rows_t,
        },
        {
            "type": "text",
            "tab": "overview",
            "title": "关键假设",
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
            "type": "text",
            "tab": "robust",
            "title": "局限与已知偏差",
            "text": (
                "- **幸存者偏差**：股票池为当前仍在交易的大盘股，已退市或长期走弱的标的未纳入，"
                "结论可能偏乐观（即真实区分度可能更差）。\n"
                "- **事件重叠**：日度事件的前瞻窗口相互重叠，胜率的标准误被低估。"
                "已做非重叠采样（每 5 个信号取 1 个，样本 1410）复核，"
                "加仓 46.2% vs 清仓 50.0%，结论一致。\n"
                "- **时点近似**：生产策略是盘中决策，回测用收盘快照近似，"
                "若信号存在日内 alpha 则会被低估；但均线主体在收盘计算无误。\n"
                "- **未复现维度**：资金面（±38）与消息面（±27）因数据不可得未参与评分，"
                "本结论检验的是「技术面主导的评分体系」——而技术面恰是生产决策的主要驱动力。"
            ),
        },
        {
            "type": "metric_table",
            "tab": "robust",
            "title": "分位数分档（检验评分本身是否有区分度）",
            "columns": ["分组", "样本数", "5日均值", "5日中位", "5日胜率"],
            "rows": rows_q,
        },
        {
            "type": "metric_table",
            "tab": "robust",
            "title": "分年度稳定性（加仓档 vs 清仓档 5 日胜率差）",
            "columns": ["年份", "加仓样本", "清仓样本", "加仓胜率", "清仓胜率", "差值"],
            "rows": rows_y,
        },
        {
            "type": "text",
            "tab": "robust",
            "title": "优化建议",
            "text": (
                "- 现有评分（均线主导）未通过有效性检验，建议按你偏好的体系重构："
                "主力资金净比、量价分层、板块情绪周期、个股相对板块强弱、价格与时间双止损。\n"
                "- 若保留均线维度，应大幅降低其权重，并用回测重新标定阈值，而非沿用 28/5/-22。\n"
                "- 信号可靠性表中样本量极低（n=1~6）的信号应停止参与打分。\n"
                "- 建议补充「资金面」历史数据源（当前接口仅当日），否则该维度无法被验证。"
            ),
        },
    ]

    report = build_dashboard_data(
        trades_csv=str(TRADES),
        summary_json=str(SUMMARY),
        trade_history=th,
        summary={
            "total_events": s["total_events"],
            "avg_return_pct": s["avg_return_pct"],
            "median_return_pct": s["median_return_pct"],
            "win_rate_pct": s["win_rate_pct"],
            "best_event_pct": s["best_event_pct"],
            "worst_event_pct": s["worst_event_pct"],
        },
        meta=summary["meta"],
        language="zh",
        market="china_a",
        event_overview_mode="stats",
        extra_modules=extra,
        ui_overrides={
            "tabs": [
                {"id": "overview", "label": "阈值检验"},
                {"id": "robust", "label": "稳健性与建议"},
                {"id": "trades", "label": "事件明细（抽样）"},
            ],
            "active_tab": "overview",
        },
    )

    # 事件明细表放到独立 tab，自定义中文列名
    report["modules"].append({
        "type": "trades_table",
        "tab": "trades",
        "title": "事件明细（每档抽样 120 条，完整数据在 CSV）",
        "rows": th,
        "columns": [
            {"key": "symbol", "label": "标的"},
            {"key": "signal_date", "label": "信号日"},
            {"key": "entry_date", "label": "买入日"},
            {"key": "exit_date", "label": "卖出日"},
            {"key": "entry_price", "label": "买入价", "format": "number"},
            {"key": "score", "label": "评分", "format": "number"},
            {"key": "档位_阈值", "label": "档位", "format": "pill"},
            {"key": "pnl_pct", "label": "5日收益", "format": "pct"},
        ],
    })

    out = ROOT / "index.html"
    render_dashboard(report, output_path=str(out), template_path=str(TEMPLATE))
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"已生成 {out}（{size_mb:.2f} MB），事件行 {len(th)} 条（抽样）")


if __name__ == "__main__":
    build()
