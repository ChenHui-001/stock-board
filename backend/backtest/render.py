"""回测看板渲染：把运行产物渲染成自包含中文 HTML。

统一走 `backend/backtest/dashboard/` 里的模板与渲染器，禁止手写独立页面。
事件研究形态不引用夏普 / 年化 / 最大回撤等组合级指标。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .dashboard.render_dashboard import build_dashboard_data, render_dashboard

TEMPLATE = Path(__file__).resolve().parent / "dashboard" / "dashboard_template.html"

DEFAULT_TABS = [
    {"id": "overview", "label": "结果概览"},
    {"id": "robust", "label": "稳健性与局限"},
    {"id": "trades", "label": "事件明细（抽样）"},
]

SAMPLE_PER_GROUP = 120


def svg_bar_chart(
    rows: list[dict[str, Any]],
    value_key: str = "5日均值%",
    win_key: str = "5日胜率%",
    name_key: str = "档位",
    value_label: str = "平均收益",
) -> str:
    """分档收益柱状图（纯 SVG，无外部依赖；A 股涨红跌绿）。"""
    if not rows:
        return ""
    names = [str(r[name_key]) for r in rows]
    vals = [float(r.get(value_key) or 0) for r in rows]
    wins = [float(r.get(win_key) or 0) for r in rows]
    lo, hi = min(min(vals), -0.05), max(max(vals), 0.05)
    span = hi - lo or 1.0
    W, H = 760, 260
    pad_l, pad_b, pad_t = 70, 46, 24
    plot_w = W - pad_l - 30
    plot_h = H - pad_b - pad_t
    zero_y = pad_t + plot_h * (hi / span)

    bars: list[str] = []
    labels: list[str] = []
    n = len(rows)
    slot = plot_w / n
    bw = slot * 0.5
    for i, (v, name, w) in enumerate(zip(vals, names, wins)):
        cx = pad_l + slot * (i + 0.5)
        y = pad_t + plot_h * ((hi - v) / span)
        h = abs(y - zero_y)
        top = min(y, zero_y)
        color = "#e5534b" if v >= 0 else "#2ea86b"
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
    axis = [
        f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{W - 30}" y2="{zero_y:.1f}" '
        f'stroke="#5a6270" stroke-width="1"/>',
        f'<text x="{pad_l - 10}" y="{pad_t + 10:.1f}" text-anchor="end" '
        f'font-size="11" fill="#8b93a1">{value_label}</text>',
    ]
    return (
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        f'aria-label="{value_label}">'
        + "".join(axis + bars + labels)
        + "</svg>"
    )


def sample_trades(trades_csv: str | Path, group_col: str | None = None,
                  per_group: int = SAMPLE_PER_GROUP) -> list[dict[str, Any]]:
    """抽样事件明细：按分组各取前 N 条，避免看板 HTML 过大。"""
    df = pd.read_csv(trades_csv)
    if df.empty:
        return []
    if group_col and group_col in df.columns:
        df = df.groupby(group_col, sort=False).head(per_group)
    else:
        df = df.head(per_group * 4)
    return df.astype(object).where(pd.notna(df), None).to_dict(orient="records")


def render_report(
    *,
    output_path: str | Path,
    trades_csv: str | Path,
    meta: dict[str, Any],
    summary: dict[str, Any],
    extra_modules: list[dict[str, Any]],
    tabs: list[dict[str, str]] | None = None,
    trade_columns: list[dict[str, str]] | None = None,
    group_col: str | None = None,
    sample_title: str = "事件明细（抽样）",
    event_overview_mode: str = "stats",
) -> Path:
    """渲染一次运行的看板 HTML，返回文件路径。"""
    trade_history = sample_trades(trades_csv, group_col=group_col)
    report = build_dashboard_data(
        trades_csv=str(trades_csv),
        summary_json=None,
        trade_history=trade_history,
        summary=summary,
        meta={
            "strategy_name": meta.get("strategy_name", "策略回测"),
            "symbol": meta.get("symbol", ""),
            "start": meta.get("start", ""),
            "end": meta.get("end", ""),
            "market": "china_a",
            "note": meta.get("note", ""),
        },
        language="zh",
        market="china_a",
        event_overview_mode=event_overview_mode,
        extra_modules=extra_modules,
        ui_overrides={
            "tabs": tabs or DEFAULT_TABS,
            "active_tab": (tabs or DEFAULT_TABS)[0]["id"],
        },
    )
    if trade_history:
        report["modules"].append({
            "type": "trades_table",
            "tab": "trades",
            "title": sample_title,
            "rows": trade_history,
            "columns": trade_columns or _default_trade_columns(trade_history[0].keys()),
        })
    out = Path(output_path)
    render_dashboard(report, output_path=str(out), template_path=str(TEMPLATE))
    return out


def _default_trade_columns(keys: Any) -> list[dict[str, str]]:
    label_map = {
        "symbol": "标的", "signal_date": "信号日", "entry_date": "买入日",
        "exit_date": "卖出日", "entry_price": "买入价", "score": "评分",
        "tech_score": "技术面", "fundamental_score": "基本面",
        "档位_阈值": "档位", "档位_分位": "分位", "pnl_pct": "收益",
        "holding_days": "持有天数", "signal": "信号", "hit": "命中",
        "next_change_pct": "次日涨跌",
    }
    fmt_map = {
        "entry_price": "number", "score": "number", "tech_score": "number",
        "fundamental_score": "number", "档位_阈值": "pill", "档位_分位": "pill",
        "pnl_pct": "pct", "next_change_pct": "pct", "hit": "pill",
    }
    cols = []
    for k in keys:
        cols.append({
            "key": k,
            "label": label_map.get(k, str(k)),
            **({"format": fmt_map[k]} if k in fmt_map else {}),
        })
    return cols


def metric_rows(
    df: pd.DataFrame, columns: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """把分档 DataFrame 转成 metric_table 行；columns = [(列名, 是否为「相对 50%」型), ...]。"""
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "metric": str(r["档位"]),
            "values": [_cell(r, c, rel) for c, rel in columns],
        })
    return rows


def _cell(row: Any, col: str, relative: bool) -> dict[str, Any]:
    if col not in row or pd.isna(row[col]):
        return {"main": "—"}
    v = float(row[col])
    if col == "样本数":
        return {"main": f"{int(v)}"}
    if col == "占比%":
        return {"main": f"{v:.1f}%"}
    if "胜率" in col:
        return {"main": f"{v:.1f}%", "raw": (v - 50) if relative else v}
    return {"main": f"{v:+.3f}%", "raw": v}


def json_dump(obj: Any, path: str | Path) -> None:
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
