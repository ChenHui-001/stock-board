"""策略三：盘中 vs 收盘时点对照实验（事件研究）。

背景：`_intraday_score` 在生产环境是**盘中实时**信号（现价实时变化），但策略二
（盘口命中率）用**日线近似收盘时点**（现价=收盘）。两者信号强弱可能不同——尤其
「高位强势/低位下跌」这类位置×方向信号，盘中触及高位与收盘站在高位含义不同。

本实验用真实 **5 分钟线**构造盘中快照（当日约 14:00 时点的现价/累计高低/累计量），
与同一交易日的收盘快照对比：

1. 打分方向一致性：两时点方向翻转的比例（按信号拆）
2. 命中率差异：各信号在两个时点下对次日涨跌的命中率
3. 校准回调建议：盘中显著更强/更弱的信号需要回调权重

执行口径：两个时点的信号都发生在 bar i 内，收益统一用 bar i+1 收盘度量，
不存在未来函数。5 分钟线优先东财（单次 1024 根≈21 交易日），限流时回退
腾讯 mkline 翻页（每页 480 根=10 交易日）；两源都失败时仅输出收盘时点并降级提示。
"""
from __future__ import annotations

import asyncio
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .. import analysis
from ..providers.base import Bar
from ..utils import normalize_code, resolve_market
from . import engine, render
from .intraday_strategy import (
    DEFAULT_CODES,
    SIGNAL_RULES,
    confidence,
    make_quote,
    signal_labels,
)

STRATEGY_ID = "intraday_compare"
STRATEGY_NAME = "盘中 vs 收盘对照"

# 盘中时点：取每个交易日倒数第 12 根 5 分钟线（约 14:00，距收盘 1 小时）
INTRADAY_OFFSET = 12
# 腾讯 mkline 单次上限（480 根 = 10 个交易日 × 48 根）
MINUTE_LIMIT = 480
MINUTE_PAGES = 3

PARAMS_SCHEMA: list[dict[str, Any]] = [
    {
        "key": "codes", "label": "股票池", "type": "textarea",
        "default": ",".join(DEFAULT_CODES[:8]),
        "hint": "6 位 A 股代码，逗号或换行分隔；分钟线较耗时，建议不超过 8 只",
    },
    {
        "key": "days", "label": "日线根数", "type": "number",
        "default": 60, "min": 30, "max": 250, "step": 10,
        "hint": "分钟线按此拉取（东财单次 1024 根≈21 日，腾讯翻页 3 页≈30 日）",
    },
]


async def _tencent_minutes(code: str, market: str, limit: int, pages: int = 1) -> list[Bar]:
    """腾讯 5 分钟线（ifzq.gtimg.cn mkline），支持翻页拉更长历史。"""
    import httpx

    symbol = f"{'sh' if market == 'SH' else 'sz'}{code}"
    url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
    all_rows: list[list] = []
    seen: set[str] = set()
    start = ""
    async with httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
    ) as client:
        for _ in range(pages):
            param = f"{symbol},m5,{start},,{limit}" if start else f"{symbol},m5,,,{limit}"
            resp = await client.get(url, params={"param": param})
            resp.raise_for_status()
            data = resp.json()
            rows = ((data.get("data") or {}).get(symbol) or {}).get("m5") or []
            if not rows:
                break
            new_rows = [r for r in rows if str(r[0]) not in seen]
            all_rows.extend(new_rows)
            seen.update(str(r[0]) for r in new_rows)
            start = str(rows[0][0])
            if len(new_rows) < limit:
                break
    if not all_rows:
        raise RuntimeError("腾讯分钟线返回为空")
    all_rows.sort(key=lambda r: str(r[0]))
    bars: list[Bar] = []
    for row in all_rows:
        if len(row) < 6:
            continue
        dt = str(row[0])
        date = f"{dt[0:4]}-{dt[4:6]}-{dt[6:8]}"
        bars.append(Bar(
            date=f"{date} {dt[8:10]}:{dt[10:12]}",
            open=float(row[1]), close=float(row[2]),
            high=float(row[3]), low=float(row[4]),
            volume=float(row[5]) * 100,
        ))
    return bars


def _samples_for_day(
    code: str, day_bars: list[Bar], minute_bars: list[Bar],
    prev_close: float, avg_vol5: float,
) -> dict[str, Any]:
    """同一交易日构造 (收盘时点, 盘中时点) 两个快照。"""
    close_bar = day_bars[-1]
    close_score, close_note = analysis._intraday_score(
        make_quote(close_bar, prev_close, avg_vol5))

    intra_score: int | None = None
    intra_note = ""
    intra_labels: list[tuple[str, bool]] = []
    if minute_bars and len(minute_bars) >= INTRADAY_OFFSET + 1:
        m = minute_bars[-INTRADAY_OFFSET]
        hi = max(b.high for b in minute_bars)
        lo = min(b.low for b in minute_bars)
        cum_vol = sum(b.volume for b in minute_bars)
        vr = (cum_vol / avg_vol5) if avg_vol5 else None
        full_vol = close_bar.volume or 0
        turnover = None
        if close_bar.turnover and full_vol:
            turnover = close_bar.turnover * (cum_vol / full_vol)
        q = {
            "price": m.close,
            "prev_close": prev_close,
            "high": hi,
            "low": lo,
            "change_pct": (m.close - prev_close) / prev_close * 100 if prev_close else None,
            "volume_ratio": vr,
            "turnover": turnover,
        }
        intra_score, intra_note = analysis._intraday_score(q)
        intra_labels = signal_labels(intra_note)

    return {
        "code": code,
        "date": close_bar.date,
        "close_score": close_score,
        "close_note": close_note,
        "close_labels": signal_labels(close_note),
        "intra_score": intra_score,
        "intra_note": intra_note,
        "intra_labels": intra_labels,
        "next_ret": 0.0,   # 由调用方回填（日线次日涨跌）
    }


async def collect_samples(
    codes: list[str], days: int, on_progress: Callable[[float, str], None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """拉日线 + 5 分钟线，逐交易日构造双时点快照。"""
    from ..providers import registry as _reg

    samples: list[dict[str, Any]] = []
    per_stock: list[dict[str, Any]] = []
    total = len(codes)
    for idx, code in enumerate(codes, 1):
        on_progress(idx / max(1, total) * 0.9, f"对照取数 {code}（{idx}/{total}）")
        market = resolve_market(code)
        em = next((p for p in _reg().providers if p.name == "eastmoney"), None)
        ths = next((p for p in _reg().providers if p.name == "ths"), None)

        # 日线：优先同花顺（省东财配额给 5 分钟线）
        day_bars_all: list[Bar] = []
        day_src = ""
        try:
            if ths is not None:
                day_bars_all = await ths.kline(code, market, days + 10)
                day_src = "ths"
        except Exception as exc:  # noqa: BLE001
            log.debug("%s 降级: %s", "collect_samples", exc)
            day_bars_all = []
        if not day_bars_all:
            try:
                day_bars_all, day_src = await engine.fetch_bars(code, market, days + 10)
            except Exception:  # noqa: BLE001
                per_stock.append({"code": code, "day_source": "失败",
                                  "minute_source": "", "usable": 0, "with_intraday": 0})
                continue

        # 5 分钟线：东财优先，失败回退腾讯翻页
        minute_src = ""
        minute_bars_all: list[Bar] = []
        if em is not None:
            for attempt in range(3):
                try:
                    minute_bars_all = await em.kline(code, market, 1024, klt=5)
                    minute_src = "eastmoney"
                    break
                except Exception as exc:  # noqa: BLE001
                    log.debug("%s 降级: %s", "collect_samples", exc)
                    if attempt < 2:
                        await asyncio.sleep(7 * (attempt + 1))
        if not minute_bars_all:
            try:
                minute_bars_all = await _tencent_minutes(code, market, MINUTE_LIMIT, MINUTE_PAGES)
                minute_src = "tencent"
            except Exception as exc:  # noqa: BLE001
                log.debug("%s 降级: %s", "collect_samples", exc)
                minute_src = ""

        day_by_date: dict[str, list[Bar]] = defaultdict(list)
        for b in day_bars_all:
            day_by_date[b.date[:10]].append(b)
        minute_by_date: dict[str, list[Bar]] = defaultdict(list)
        for b in minute_bars_all:
            minute_by_date[b.date[:10]].append(b)

        dates = sorted(day_by_date)
        usable = 0
        with_intra = 0
        for i in range(6, len(dates) - 1):
            d, nxt = dates[i], dates[i + 1]
            bars = day_by_date[d]
            if not bars:
                continue
            close_bar = bars[-1]
            prev_close = day_by_date[dates[i - 1]][-1].close
            avg_vol5 = statistics.mean(day_by_date[dates[j]][-1].volume for j in range(i - 5, i))
            nxt_close = day_by_date[nxt][-1].close
            if not (prev_close and close_bar.close and nxt_close):
                continue
            s = _samples_for_day(code, bars, minute_by_date.get(d, []), prev_close, avg_vol5)
            s["next_ret"] = (nxt_close - close_bar.close) / close_bar.close * 100
            samples.append(s)
            usable += 1
            if s["intra_score"] is not None:
                with_intra += 1
        per_stock.append({"code": code, "day_source": day_src,
                          "minute_source": minute_src or "无",
                          "usable": usable, "with_intraday": with_intra})
    return samples, per_stock


def _hit_rate(items: list[dict[str, Any]], bullish: bool) -> tuple[float, int]:
    if not items:
        return 0.0, 0
    hit = sum(1 for s in items if (s["next_ret"] > 0) == bullish)
    return hit / len(items), len(items)


def _dir_rows(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """两时点方向一致性：按信号拆翻转率。"""
    flip_by: dict[str, int] = defaultdict(int)
    same_by: dict[str, int] = defaultdict(int)
    flip = same = 0
    for s in samples:
        if s["intra_score"] is None:
            continue
        c, i = s["close_score"], s["intra_score"]
        cd = 1 if c > 0 else (-1 if c < 0 else 0)
        idd = 1 if i > 0 else (-1 if i < 0 else 0)
        if cd == 0 or idd == 0:
            continue
        bucket = same_by if cd == idd else flip_by
        if cd == idd:
            same += 1
        else:
            flip += 1
        for label, _b in s["close_labels"]:
            bucket[label] += 1
    rows: list[dict[str, Any]] = []
    for label, _fn, _b in SIGNAL_RULES:
        f, t = flip_by.get(label, 0), flip_by.get(label, 0) + same_by.get(label, 0)
        if t < 20:
            continue
        rows.append({
            "信号": label, "可比样本": t, "方向翻转": f,
            "翻转率": f"{f / t * 100:.0f}%", "_rate": round(f / t * 100, 1),
        })
    return rows, same, flip


def _cmp_rows(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """各信号：收盘 vs 盘中命中率。"""
    close_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    intra_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in samples:
        for label, bullish in s["close_labels"]:
            close_group[label].append({**s, "bullish": bullish})
        for label, bullish in s["intra_labels"]:
            intra_group[label].append({**s, "bullish": bullish})
    rows: list[dict[str, Any]] = []
    for label, _fn, bullish in SIGNAL_RULES:
        cg, ig = close_group.get(label, []), intra_group.get(label, [])
        if not cg and not ig:
            continue
        c_rate, c_n = _hit_rate(cg, bullish)
        i_rate, i_n = _hit_rate(ig, bullish)
        diff = (i_rate - c_rate) * 100
        if c_n >= 30 and i_n >= 30:
            if diff >= 5:
                conclusion = "盘中更强 → 日线校准低估，需回调"
            elif diff <= -5:
                conclusion = "盘中更弱 → 校准方向安全，可维持"
            else:
                conclusion = "差异不显著"
        elif i_n < 30:
            conclusion = "盘中样本不足"
        else:
            conclusion = "样本不足"
        rows.append({
            "信号": label, "方向": "看多" if bullish else "看空",
            "收盘样本": c_n, "收盘命中": f"{c_rate * 100:.1f}%" if c_n else "—",
            "盘中样本": i_n, "盘中命中": f"{i_rate * 100:.1f}%" if i_n else "—",
            "差异": f"{diff:+.1f}pct" if (c_n and i_n) else "—",
            "结论": conclusion,
            "_diff": round(diff, 1) if (c_n and i_n) else 0.0,
        })
    return rows


async def run(params: dict[str, Any], on_progress: Callable[[float, str], None]) -> dict[str, Any]:
    raw_codes = params.get("codes") or ",".join(DEFAULT_CODES[:8])
    codes = [c for c in (normalize_code(x) for x in engine.normalize_codes(raw_codes)) if c]
    if not codes:
        raise ValueError("股票池为空，请至少填写一只标的代码")
    days = int(params.get("days") or 60)
    days = max(30, min(250, days))

    on_progress(0.05, f"准备 {len(codes)} 只标的（日线 + 5 分钟线）")
    samples, per_stock = await collect_samples(codes, days, on_progress)
    if not samples:
        raise RuntimeError("无有效样本：请检查网络 / 数据源，或减少股票池")

    total = len(samples)
    with_intra = sum(1 for s in samples if s["intra_score"] is not None)
    base_up = sum(1 for s in samples if s["next_ret"] > 0) / total
    degraded = with_intra == 0

    dir_rows, same, flip = _dir_rows(samples)
    cmp_rows = _cmp_rows(samples)

    rows: list[dict[str, Any]] = []
    for s in samples:
        labels = s["close_labels"] or s["intra_labels"]
        bullish = labels[0][1] if labels else True
        rows.append({
            "symbol": s["code"],
            "signal_date": str(s["date"])[:10],
            "entry_date": str(s["date"])[:10],
            "exit_date": "",
            "entry_price": None,
            "score": s["close_score"],
            "盘中分": s["intra_score"] if s["intra_score"] is not None else "",
            "信号": "、".join(lb for lb, _ in labels) or "无",
            "方向": "看多" if bullish else "看空",
            "hit": "命中" if (s["next_ret"] > 0) == bullish else "未中",
            "pnl_pct": round(s["next_ret"], 3),
            "holding_days": 1,
            "label": "收盘：" + (s["close_note"] or "无"),
        })
    trades = pd.DataFrame(rows)

    engine.prune_run_dirs()   # 顺带清理过期运行目录
    tmpdir = Path(tempfile.mkdtemp(prefix="bt_cmp_", dir=str(engine.CACHE_DIR)))
    trades_csv = tmpdir / "trades.csv"
    trades.to_csv(trades_csv, index=False, encoding="utf-8-sig")

    meta = {
        "strategy_name": f"{STRATEGY_NAME}实验（事件研究）",
        "symbol": f"{len(codes)} 只 A 股面板",
        "start": str(trades["signal_date"].min()) if not trades.empty else "",
        "end": str(trades["signal_date"].max()) if not trades.empty else "",
        "note": ("盘中时点≈14:00 真实 5 分钟线快照；" +
                 ("⚠ 分钟线源全部失败，仅收盘时点可用" if degraded else "含盘中时点")),
    }
    stats = engine.event_stats(pd.to_numeric(trades["pnl_pct"], errors="coerce"))
    stats["win_rate_pct"] = round(base_up * 100, 1)
    full_summary = {
        "meta": meta,
        "summary": {**stats, "with_intraday": with_intra, "degraded": degraded},
        "direction": dir_rows,
        "compare": cmp_rows,
        "per_stock": per_stock,
    }

    on_progress(0.93, "生成看板")
    report_html = tmpdir / "report.html"
    render.render_report(
        output_path=report_html,
        trades_csv=trades_csv,
        meta=meta,
        summary=stats,
        extra_modules=_dashboard_modules(dir_rows, cmp_rows, total, with_intra,
                                         base_up, same, flip, degraded),
        tabs=[
            {"id": "overview", "label": "命中率对比"},
            {"id": "robust", "label": "局限与建议"},
            {"id": "trades", "label": "事件明细（抽样）"},
        ],
        trade_columns=[
            {"key": "symbol", "label": "标的"},
            {"key": "signal_date", "label": "信号日"},
            {"key": "score", "label": "收盘分", "format": "number"},
            {"key": "盘中分", "label": "盘中分", "format": "number"},
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
        "tables": {"direction": dir_rows, "compare": cmp_rows, "per_stock": per_stock},
        "trades_csv": trades_csv,
        "report_html": report_html,
        "meta": meta,
    }


def _dashboard_modules(
    dir_rows: list[dict[str, Any]], cmp_rows: list[dict[str, Any]],
    total: int, with_intra: int, base_up: float, same: int, flip: int, degraded: bool,
) -> list[dict[str, Any]]:
    total_dir = same + flip
    flip_pct = (flip / total_dir * 100) if total_dir else 0.0
    head = (
        f"- 样本 **{total}** 个（含盘中时点 **{with_intra}** 个），"
        f"基线次日上涨率 **{base_up * 100:.1f}%**。\n"
    )
    if degraded:
        head += ("- ⚠️ **分钟线数据源全部失败**，本次只有收盘时点结果，"
                 "盘中 vs 收盘对照无法完成。\n")
    elif total_dir:
        head += (f"- 两时点都有明确方向的样本 {total_dir} 个：方向一致 {same} "
                 f"（{100 - flip_pct:.1f}%），**方向翻转 {flip}（{flip_pct:.1f}%）**。\n")

    recalled = [r for r in cmp_rows if "回调" in r["结论"]]
    weaker = [r for r in cmp_rows if "更弱" in r["结论"]]
    if recalled:
        head += "- 需要盘中回调的信号：" + "、".join(r["信号"] for r in recalled) + "。\n"
    if weaker:
        head += "- 盘中反而更弱（校准安全）的信号：" + "、".join(r["信号"] for r in weaker) + "。\n"

    modules: list[dict[str, Any]] = [
        {"type": "text", "tab": "overview", "title": "结论", "text": head},
        {
            "type": "metric_table", "tab": "overview",
            "title": "信号命中率对比（收盘时点 vs 盘中 14:00 时点）",
            "columns": ["信号", "方向", "收盘样本", "收盘命中", "盘中样本", "盘中命中", "差异", "结论"],
            "rows": [
                {
                    "metric": r["信号"],
                    "values": [
                        {"main": r["方向"]}, {"main": str(r["收盘样本"])},
                        {"main": r["收盘命中"]}, {"main": str(r["盘中样本"])},
                        {"main": r["盘中命中"]}, {"main": r["差异"], "raw": r["_diff"]},
                        {"main": r["结论"]},
                    ],
                }
                for r in cmp_rows
            ],
        },
    ]
    if dir_rows:
        modules.append({
            "type": "metric_table", "tab": "overview",
            "title": "两时点打分方向一致性（按收盘触发信号统计，样本≥20）",
            "columns": ["信号", "可比样本", "方向翻转", "翻转率"],
            "rows": [
                {
                    "metric": r["信号"],
                    "values": [
                        {"main": str(r["可比样本"])}, {"main": str(r["方向翻转"])},
                        {"main": r["翻转率"], "raw": -r["_rate"]},
                    ],
                }
                for r in dir_rows
            ],
        })
    modules += [
        {
            "type": "text", "tab": "robust", "title": "关键假设",
            "text": (
                "- 盘中时点 = 当日**倒数第 12 根 5 分钟线**（约 14:00），"
                "现价取该根收盘，累计高低/累计量取当日已走完的部分。\n"
                "- 盘中量比 = 当日累计成交量 / 前 5 日均量（比收盘口径偏小，符合真实盘中特征）；"
                "盘中换手按累计量占全天量的比例折算。\n"
                "- 两个时点信号都在 bar i 内产生，收益统一用 bar i+1 收盘度量，无未来函数。\n"
                "- 5 分钟线优先东财（1024 根≈21 交易日），限流时回退腾讯 mkline 翻页（≈30 交易日）。"
            ),
        },
        {
            "type": "text", "tab": "robust", "title": "局限与已知偏差",
            "text": (
                "- **样本量小**：分钟线历史长度受限（东财单只只能拉约 21 个交易日），"
                "各信号在盘中时点的样本通常远少于日线回测，n<30 仅作参考。\n"
                "- **幸存者偏差**：股票池为当前仍在交易的大盘股。\n"
                "- **时点固定**：只对照 14:00 一个时点，不能代表全天所有时点；"
                "开盘 / 尾盘的信号特征可能不同。\n"
                "- **不计成本**：事件研究不计手续费与滑点。"
            ),
        },
        {
            "type": "text", "tab": "robust", "title": "校准建议",
            "text": (
                "- 盘中命中率 ≥ 收盘 +5pct：日线校准低估了盘中信号，建议回调该信号权重。\n"
                "- 盘中命中率 ≤ 收盘 −5pct：校准方向安全，可维持甚至加强。\n"
                "- 差异在 ±5pct 内：视为不显著，维持现状。\n"
                "- 任一时点样本 <30：不参与校准决策。"
            ),
        },
    ]
    return modules


def selftest() -> int:
    """离线自测：合成日线 + 合成分钟线，验证结构完整（不触网）。"""
    day_by_date: dict[str, list[Bar]] = defaultdict(list)
    minute_by_date: dict[str, list[Bar]] = defaultdict(list)
    base = 10.0
    for i in range(1, 25):
        d = f"2026-07-{i:02d}"
        close = base + (1.0 if i % 2 == 0 else -0.8)
        day_by_date[d] = [Bar(date=d, open=close - 0.1, close=close, high=close + 0.5,
                              low=close - 0.5, volume=2e7, turnover=1.2,
                              change_pct=(close - base) / base * 100)]
        mins = []
        for k in range(48):
            hh = 9 + k // 12 if k < 24 else 13 + (k - 24) // 12
            mm = 30 + (k % 12) * 5 if k < 24 else 5 + (k % 12) * 5
            mins.append(Bar(date=f"{d} {hh:02d}:{mm:02d}", open=close - 0.05,
                            close=close + (0.05 if k < 36 else -0.03),
                            high=close + 0.1, low=close - 0.1, volume=4e5))
        minute_by_date[d] = mins
        base = close
    dates = sorted(day_by_date)
    samples: list[dict[str, Any]] = []
    for i in range(6, len(dates) - 1):
        d = dates[i]
        prev_close = day_by_date[dates[i - 1]][-1].close
        avg5 = statistics.mean(day_by_date[dates[j]][-1].volume for j in range(i - 5, i))
        s = _samples_for_day("600000", day_by_date[d], minute_by_date[d], prev_close, avg5)
        nxt = day_by_date[dates[i + 1]][-1].close
        s["next_ret"] = (nxt - day_by_date[d][-1].close) / day_by_date[d][-1].close * 100
        samples.append(s)
    assert len(samples) > 10, "自测样本不足"
    assert all("close_score" in s and "intra_score" in s for s in samples)
    mods = _dashboard_modules(_dir_rows(samples)[0], _cmp_rows(samples), len(samples),
                              sum(1 for s in samples if s["intra_score"] is not None),
                              0.5, 1, 1, False)
    assert mods and mods[0]["title"] == "结论"
    print(f"对照实验自测通过（{len(samples)} 个合成样本，含盘中/收盘两时点）")
    return 0
