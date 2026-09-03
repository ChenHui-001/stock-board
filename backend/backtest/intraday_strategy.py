"""策略二：盘口分项信号命中率检验（事件研究）。

直接用生产代码 `analysis._intraday_score` 逐日打分，统计每个盘口信号（盘中位置×
涨跌、量比、振幅、换手）触发后「次日」涨跌方向与幅度，输出命中率与权重校准建议。

用**日线近似盘中快照（收盘时点）**：现价=当日收盘、盘中位置=(close-low)/(high-low)、
量比=当日成交量/前 5 日均量、换手率=当日换手、涨跌幅=当日涨跌幅。

命中定义：看多信号（高位强势/低位回升/放量上攻/缩量下跌/振幅收敛/换手活跃）
次日上涨为命中；看空信号次日下跌为命中。

执行口径：信号在 bar i 收盘产生，bar i+1 收盘度量收益（与生产「次日」语义一致），
不模拟盘中成交，属事件研究。
"""
from __future__ import annotations

import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .. import analysis
from ..utils import confidence as _confidence, normalize_code, resolve_market
from . import engine, render

STRATEGY_ID = "intraday_signal"
STRATEGY_NAME = "盘口信号命中率"

DEFAULT_CODES = [
    "600000", "600036", "600519", "601318", "601398", "000001",
    "000333", "000858", "002594", "300750", "600887", "601899",
]

# 信号分组：从 _intraday_score 返回的说明文本按关键词提取（标签, 匹配函数, 看多?）
SIGNAL_RULES: list[tuple[str, Any, bool]] = [
    ("高位强势", lambda n: "当日高位" in n and "强势" in n, True),
    ("冲高回落", lambda n: "高位" in n and "回落" in n, False),
    ("低位回升", lambda n: "低位" in n and "回升" in n, True),
    ("低位下跌", lambda n: "低位" in n and "下跌" in n, False),
    ("放量上攻", lambda n: "放量上攻" in n, True),
    ("放量下挫", lambda n: "放量下挫" in n, False),
    ("缩量上涨", lambda n: "缩量上涨" in n, False),
    ("缩量下跌", lambda n: "缩量下跌" in n, True),
    ("振幅剧烈", lambda n: "振幅" in n and "剧烈" in n, False),
    ("振幅收敛", lambda n: "振幅" in n and "收敛" in n, True),
    ("换手活跃", lambda n: "交投活跃" in n, True),
    ("换手出货", lambda n: "分歧出货" in n, False),
    ("交投清淡", lambda n: "交投清淡" in n, False),
]

PARAMS_SCHEMA: list[dict[str, Any]] = [
    {
        "key": "codes", "label": "股票池", "type": "textarea",
        "default": ",".join(DEFAULT_CODES),
        "hint": "6 位 A 股代码，逗号或换行分隔",
    },
    {
        "key": "days", "label": "日线根数", "type": "number",
        "default": 250, "min": 60, "max": 1000, "step": 10,
        "hint": "每只标的回测多少根日线；250 根约 1 年",
    },
]


def make_quote(bar: Any, prev_close: float, avg_vol5: float) -> dict[str, Any]:
    """用日线近似盘中快照（收盘时点），字段对齐 _intraday_score 的入参。"""
    return {
        "price": bar.close,
        "prev_close": prev_close,
        "high": bar.high,
        "low": bar.low,
        "change_pct": bar.change_pct,
        "volume_ratio": (bar.volume / avg_vol5) if avg_vol5 else None,
        "turnover": bar.turnover,
    }


def signal_labels(note: str) -> list[tuple[str, bool]]:
    return [(label, bullish) for label, fn, bullish in SIGNAL_RULES if fn(note)]


def calibrate(hit_rate: float, base_rate: float, n: int) -> str:
    """基于命中率与基线上涨率的偏离给出校准建议。"""
    if n < 50:
        return "样本不足，暂不调整"
    delta = hit_rate - base_rate
    if delta >= 0.05:
        return "信号有效（高于基线 5pct+），可维持或上调权重"
    if delta >= 0.02:
        return "信号有效，权重可维持"
    if delta >= -0.03:
        return "信号偏弱，建议下调权重或并入其他信号"
    return "信号反向/无效（低于基线 3pct+），建议大幅下调或检查方向"


def confidence(n: int) -> tuple[str, str]:
    """按样本量标注统计置信度（共享实现 backend.utils.confidence）。"""
    c = _confidence(n)
    return c["label"], c["note"]


# 总分分桶：看评分方向是否单调
SCORE_BUCKETS: list[tuple[str, Callable[[float], bool]]] = [
    ("≥ +6", lambda s: s >= 6),
    ("+3~+5", lambda s: 3 <= s <= 5),
    ("+1~+2", lambda s: 1 <= s <= 2),
    ("0", lambda s: s == 0),
    ("-1~-2", lambda s: -2 <= s <= -1),
    ("-3~-5", lambda s: -5 <= s <= -3),
    ("≤ -6", lambda s: s <= -6),
]


async def collect_samples(
    codes: list[str], days: int, on_progress: Callable[[float, str], None]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """逐标的取日线并打分，返回 (samples, per_stock)。"""
    samples: list[dict[str, Any]] = []
    per_stock: list[dict[str, Any]] = []
    total = len(codes)
    for idx, code in enumerate(codes, 1):
        on_progress(idx / max(1, total) * 0.9, f"取数打分 {code}（{idx}/{total}）")
        market = resolve_market(code)
        try:
            bars, src = await engine.fetch_bars(code, market, days + 10)
        except Exception as exc:  # noqa: BLE001
            per_stock.append({"code": code, "source": f"失败({exc})", "usable": 0})
            continue
        if len(bars) < 30:
            per_stock.append({"code": code, "source": src or "空", "usable": 0})
            continue
        usable = 0
        for i in range(6, len(bars) - 1):
            bar = bars[i]
            prev_close = bars[i - 1].close
            avg_vol5 = statistics.mean(b.volume for b in bars[i - 5:i])
            if not (prev_close and bar.close and bar.high and bar.low):
                continue
            q = make_quote(bar, prev_close, avg_vol5)
            score, note = analysis._intraday_score(q)
            nxt = bars[i + 1]
            if not (nxt.close and bar.close):
                continue
            next_ret = (nxt.close - bar.close) / bar.close * 100
            samples.append({
                "code": code,
                "date": bar.date,
                "score": score,
                "note": note,
                "next_ret": next_ret,
                "labels": signal_labels(note),
            })
            usable += 1
        per_stock.append({"code": code, "source": src, "usable": usable})
    return samples, per_stock


def _signal_rows(samples: list[dict[str, Any]], base_up: float) -> list[dict[str, Any]]:
    """各信号分组的命中率 / 次日涨率 / 平均涨跌 / 置信度 / 校准建议。"""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in samples:
        for label, bullish in s["labels"]:
            grouped[label].append({**s, "bullish": bullish})
    rows: list[dict[str, Any]] = []
    for label, _fn, bullish in SIGNAL_RULES:
        sub = grouped.get(label, [])
        n = len(sub)
        if not n:
            rows.append({"信号": label, "方向": "看多" if bullish else "看空",
                         "样本": 0, "命中率": "—", "次日涨率": "—",
                         "平均涨跌": "—", "置信": "—", "校准建议": "未触发", "_hit": None})
            continue
        hit = sum(1 for s in sub if (s["next_ret"] > 0) == s["bullish"])
        hit_rate = hit / n
        up = sum(1 for s in sub if s["next_ret"] > 0) / n
        avg = statistics.mean(s["next_ret"] for s in sub)
        level, _note = confidence(n)
        rows.append({
            "信号": label, "方向": "看多" if bullish else "看空", "样本": n,
            "命中率": f"{hit_rate * 100:.1f}%", "次日涨率": f"{up * 100:.1f}%",
            "平均涨跌": f"{avg:+.2f}%", "置信": level,
            "校准建议": calibrate(hit_rate, base_up, n),
            "_hit": round(hit_rate * 100, 1),
            "_base": round(base_up * 100, 1),
        })
    return rows


def _bucket_rows(samples: list[dict[str, Any]], base_up: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, fn in SCORE_BUCKETS:
        sub = [s for s in samples if fn(s["score"])]
        if not sub:
            continue
        up = sum(1 for s in sub if s["next_ret"] > 0) / len(sub)
        avg = statistics.mean(s["next_ret"] for s in sub)
        level, _note = confidence(len(sub))
        rows.append({
            "档位": label, "样本数": len(sub),
            "次日上涨率": f"{up * 100:.1f}%", "vs基线": f"{(up - base_up) * 100:+.1f}pct",
            "平均涨跌": f"{avg:+.2f}%", "置信": level,
            "_up": round(up * 100, 1), "_delta": round((up - base_up) * 100, 1),
        })
    return rows


async def run(params: dict[str, Any], on_progress: Callable[[float, str], None]) -> dict[str, Any]:
    raw_codes = params.get("codes") or ",".join(DEFAULT_CODES)
    codes = [c for c in (normalize_code(x) for x in engine.normalize_codes(raw_codes)) if c]
    if not codes:
        raise ValueError("股票池为空，请至少填写一只标的代码")
    days = int(params.get("days") or 250)
    days = max(60, min(1000, days))

    on_progress(0.05, f"准备 {len(codes)} 只标的 × {days} 根日线")
    samples, per_stock = await collect_samples(codes, days, on_progress)
    if not samples:
        raise RuntimeError("无有效样本：请检查网络 / 数据源，或扩大股票池")

    total = len(samples)
    base_up = sum(1 for s in samples if s["next_ret"] > 0) / total
    base_avg = statistics.mean(s["next_ret"] for s in samples)
    if total >= 400:
        conf = ("高", f"样本 {total} 个（深样本），结论较可靠")
    elif total >= 150:
        conf = ("中", f"样本 {total} 个，参考价值一般")
    else:
        conf = ("低", f"样本 {total} 个，仅作参考")

    sig_rows = _signal_rows(samples, base_up)
    bkt_rows = _bucket_rows(samples, base_up)

    # 事件明细：一条信号日一行（pnl_pct 用次日涨跌，正=看多命中方向为正）
    rows: list[dict[str, Any]] = []
    for s in samples:
        labels = s["labels"]
        bullish = labels[0][1] if labels else True
        rows.append({
            "symbol": s["code"],
            "signal_date": str(s["date"])[:10],
            "entry_date": str(s["date"])[:10],
            "exit_date": "",
            "entry_price": None,
            "score": s["score"],
            "信号": "、".join(lb for lb, _ in labels) or "无",
            "方向": "看多" if bullish else "看空",
            "hit": "命中" if (s["next_ret"] > 0) == bullish else "未中",
            "pnl_pct": round(s["next_ret"], 3),
            "holding_days": 1,
            "label": "、".join(lb for lb, _ in labels) or "无信号",
        })
    trades = pd.DataFrame(rows)

    engine.prune_run_dirs()   # 顺带清理过期运行目录
    tmpdir = Path(tempfile.mkdtemp(prefix="bt_intraday_", dir=str(engine.CACHE_DIR)))
    trades_csv = tmpdir / "trades.csv"
    trades.to_csv(trades_csv, index=False, encoding="utf-8-sig")

    meta = {
        "strategy_name": f"{STRATEGY_NAME}检验（事件研究）",
        "symbol": f"{len(codes)} 只 A 股面板",
        "start": str(trades["signal_date"].min()) if not trades.empty else "",
        "end": str(trades["signal_date"].max()) if not trades.empty else "",
        "note": "日线近似收盘时点；命中=信号方向与次日方向一致",
    }
    stats = engine.event_stats(pd.to_numeric(trades["pnl_pct"], errors="coerce"))
    stats["win_rate_pct"] = round(base_up * 100, 1)   # 基线次日上涨率更有意义
    full_summary = {
        "meta": meta,
        "summary": {
            **stats,
            "baseline_up_rate_pct": round(base_up * 100, 1),
            "baseline_avg_pct": round(base_avg, 3),
            "confidence": conf[0],
            "confidence_note": conf[1],
        },
        "signals": sig_rows,
        "buckets": bkt_rows,
        "per_stock": per_stock,
    }

    on_progress(0.93, "生成看板")
    report_html = tmpdir / "report.html"
    render.render_report(
        output_path=report_html,
        trades_csv=trades_csv,
        meta=meta,
        summary=stats,
        extra_modules=_dashboard_modules(sig_rows, bkt_rows, base_up, base_avg, total, conf),
        tabs=[
            {"id": "overview", "label": "信号命中率"},
            {"id": "robust", "label": "局限与建议"},
            {"id": "trades", "label": "事件明细（抽样）"},
        ],
        trade_columns=[
            {"key": "symbol", "label": "标的"},
            {"key": "signal_date", "label": "信号日"},
            {"key": "score", "label": "盘口分", "format": "number"},
            {"key": "信号", "label": "触发信号"},
            {"key": "方向", "label": "方向"},
            {"key": "hit", "label": "命中", "format": "pill"},
            {"key": "pnl_pct", "label": "次日涨跌", "format": "pct"},
        ],
        sample_title="事件明细（抽样）",
    )
    on_progress(1.0, "完成")

    return {
        "summary": stats,
        "full_summary": full_summary,
        "tables": {"signals": sig_rows, "buckets": bkt_rows, "per_stock": per_stock},
        "trades_csv": trades_csv,
        "report_html": report_html,
        "meta": meta,
    }


def _dashboard_modules(
    sig_rows: list[dict[str, Any]], bkt_rows: list[dict[str, Any]],
    base_up: float, base_avg: float, total: int, conf: tuple[str, str],
) -> list[dict[str, Any]]:
    valid = [r for r in sig_rows if r["_hit"] is not None and r["样本"] >= 50]
    best = max(valid, key=lambda r: r["_hit"]) if valid else None
    worst = min(valid, key=lambda r: r["_hit"]) if valid else None
    low_n = [r["信号"] for r in sig_rows if 0 < r["样本"] < 50]

    conclusion = (
        f"- 有效样本 **{total}** 个，置信度 **{conf[0]}**（{conf[1]}）。\n"
        f"- 基线：次日上涨率 **{base_up * 100:.1f}%**，次日平均涨跌 **{base_avg:+.2f}%**。\n"
    )
    if best:
        conclusion += (f"- 样本充足（≥50）的信号里，命中率最高的是 **{best['信号']}** "
                       f"（{best['命中率']}，基线 {best['_base']:.1f}%）；")
    if worst:
        conclusion += (f"最低的是 **{worst['信号']}**（{worst['命中率']}）。\n")
    if low_n:
        conclusion += f"- ⚠️ 样本量 <50 的信号（{('、'.join(low_n))}）结论不可靠，建议停止参与打分。\n"

    return [
        {"type": "text", "tab": "overview", "title": "结论", "text": conclusion},
        {
            "type": "metric_table", "tab": "overview",
            "title": "各信号命中率（命中 = 信号方向与次日方向一致）",
            "columns": ["信号", "方向", "样本", "命中率", "次日涨率", "平均涨跌", "置信", "校准建议"],
            "rows": [
                {
                    "metric": r["信号"],
                    "values": [
                        {"main": r["方向"]}, {"main": str(r["样本"])},
                        {"main": r["命中率"], "raw": (r["_hit"] - r["_base"])
                         if r["_hit"] is not None else 0},
                        {"main": r["次日涨率"]}, {"main": r["平均涨跌"]},
                        {"main": r["置信"]}, {"main": r["校准建议"]},
                    ],
                }
                for r in sig_rows
            ],
        },
        {
            "type": "metric_table", "tab": "overview",
            "title": "盘口总分分桶 vs 次日表现（检验单调性）",
            "columns": ["分桶", "样本数", "次日上涨率", "vs 基线", "平均涨跌", "置信"],
            "rows": [
                {
                    "metric": r["档位"],
                    "values": [
                        {"main": str(r["样本数"])},
                        {"main": r["次日上涨率"], "raw": r["_up"] - 50},
                        {"main": r["vs基线"], "raw": r["_delta"]},
                        {"main": r["平均涨跌"]},
                        {"main": r["置信"]},
                    ],
                }
                for r in bkt_rows
            ],
        },
        {
            "type": "text", "tab": "robust", "title": "关键假设",
            "text": (
                "- 用**日线近似盘中快照（收盘时点）**：现价=当日收盘、盘中位置=(收盘-最低)/(最高-最低)、"
                "量比=当日成交量 / 前 5 日均量（严格不含当日）、换手率=当日换手。\n"
                "- 信号在 bar i 收盘产生，用 bar i+1 收盘度量「次日」收益，不模拟盘中成交。\n"
                "- 命中率以「信号方向 × 次日方向一致」计；基线 = 全样本次日上涨率。\n"
                "- 不计手续费与滑点；只统计价格变动百分比。"
            ),
        },
        {
            "type": "text", "tab": "robust", "title": "局限与已知偏差",
            "text": (
                "- **时点偏差**：生产环境 `_intraday_score` 是盘中实时信号，本回测用收盘快照近似，"
                "「高位强势/冲高回落」这类位置信号在盘中与收盘含义不同，结论需谨慎外推。\n"
                "- **样本不均**：各信号触发频次差异极大，低频信号（n<50）的命中率噪声很大。\n"
                "- **事件重叠**：相邻交易日的信号样本存在重叠，标准误被低估。\n"
                "- **幸存者偏差**：股票池为当前仍在交易的大盘股。"
            ),
        },
        {
            "type": "text", "tab": "robust", "title": "校准建议",
            "text": (
                "- 命中率 ≥ 基线 +5pct：信号有效，可维持或上调权重。\n"
                "- 命中率在基线 ±2~3pct 内：信号偏弱，建议下调权重或并入其他信号。\n"
                "- 命中率 < 基线 −3pct：信号反向/无效，建议大幅下调或检查方向定义。\n"
                "- 样本 <50 的信号：不参与校准，建议在生产评分中停用。"
            ),
        },
    ]


def selftest() -> int:
    """离线自测：用合成日线验证打分与统计链路（不触网）。"""
    from ..providers.base import Bar

    bars = []
    base = 10.0
    for i in range(1, 60):
        close = base + (1.0 if i % 2 == 0 else -0.8)
        vol = 2e7 if i % 3 == 0 else 1e7
        bars.append(Bar(
            date=f"2026-01-{i:02d}", open=close - 0.1, close=close,
            high=close + 0.5, low=close - 0.5, volume=vol, turnover=1.2,
            change_pct=(close - base) / base * 100,
        ))
        base = close
    samples = []
    for i in range(6, len(bars) - 1):
        bar, prev = bars[i], bars[i - 1]
        avg5 = statistics.mean(b.volume for b in bars[i - 5:i])
        score, note = analysis._intraday_score(make_quote(bar, prev.close, avg5))
        nxt = bars[i + 1]
        samples.append({"score": score, "note": note,
                        "next_ret": (nxt.close - bar.close) / bar.close * 100,
                        "labels": signal_labels(note)})
    assert len(samples) > 40, "自测样本不足"
    assert all(isinstance(s["score"], int) and isinstance(s["next_ret"], float)
               for s in samples)
    print(f"盘口回测自测通过（{len(samples)} 个合成样本）")
    return 0
