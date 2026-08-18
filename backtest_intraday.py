"""盘口分项离线回测：验证盘中位置/量比/振幅/换手信号对次日涨跌的预测能力。

直接用生产代码 `analysis._intraday_score` 逐日打分，统计每个信号触发后
「次日」涨跌方向与幅度，输出命中率与权重校准建议。

用日线近似盘中快照（收盘时点）：现价=当日收盘、盘中位置=(close-low)/(high-low)、
量比=当日成交量/前5日均量、换手率=当日换手、涨跌幅=当日涨跌幅。
命中定义：看多信号（高位强势/低位回升/放量上攻/缩量下跌/振幅收敛/换手活跃）
次日上涨为命中；看空信号（冲高回落/低位下跌/放量下挫/缩量上涨/振幅剧烈/
换手出货/交投清淡）次日下跌为命中。

用法:
    python backtest_intraday.py                        # 默认股票池 + 250 根日线
    python backtest_intraday.py --codes 600000,601179 --days 200
    python backtest_intraday.py --json out.json        # 额外输出机器可读报告
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend import analysis  # noqa: E402
from backend.providers import registry  # noqa: E402
from backend.providers.base import Bar  # noqa: E402
from backend.utils import normalize_code, resolve_market  # noqa: E402

# 默认股票池：沪深主板/创业板代表性标的
DEFAULT_CODES = [
    "600000", "600036", "600519", "601318", "601398", "000001",
    "000333", "000858", "002594", "300750", "600887", "601899",
]

# 信号分组：从 _intraday_score 返回的说明文本按关键词提取
# (标签, 匹配函数, 看多?)
SIGNAL_RULES: list[tuple[str, Any, bool]] = [
    ("高位强势",   lambda n: "当日高位" in n and "强势" in n,  True),
    ("冲高回落",   lambda n: "高位" in n and "回落" in n,     False),
    ("低位回升",   lambda n: "低位" in n and "回升" in n,     True),
    ("低位下跌",   lambda n: "低位" in n and "下跌" in n,     False),
    ("放量上攻",   lambda n: "放量上攻" in n,                  True),
    ("放量下挫",   lambda n: "放量下挫" in n,                  False),
    ("缩量上涨",   lambda n: "缩量上涨" in n,                  False),
    ("缩量下跌",   lambda n: "缩量下跌" in n,                  True),
    ("振幅剧烈",   lambda n: "振幅" in n and "剧烈" in n,     False),
    ("振幅收敛",   lambda n: "振幅" in n and "收敛" in n,     True),
    ("换手活跃",   lambda n: "交投活跃" in n,                  True),
    ("换手出货",   lambda n: "分歧出货" in n,                  False),
    ("交投清淡",   lambda n: "交投清淡" in n,                  False),
]


def make_quote(bar: Bar, prev_close: float, avg_vol5: float) -> dict:
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


async def fetch_bars(code: str, days: int) -> tuple[str, list[Bar]]:
    market = resolve_market(code)
    try:
        bars, src = await registry().kline(code, market, days + 10)
        return src, bars
    except Exception as exc:  # noqa: BLE001
        return f"失败({exc})", []


async def run_backtest(codes: list[str], days: int, verbose: bool = False) -> dict:
    """核心回测：拉日线逐日打分，统计次日表现。供 CLI / 数据源自检 / 面板复用。"""
    samples: list[dict] = []
    per_stock: list[dict] = []
    for code in codes:
        src, bars = await fetch_bars(code, days)
        if len(bars) < 30:
            if verbose:
                print(f"  [{code}] K线不足: {src}")
            continue
        # 需要前一根收盘价与前 5 日均量，且要留一根给「次日」
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
        if verbose:
            print(f"  [{code}] 源={src} 有效样本={usable}")

    return {"per_stock": per_stock, "samples": samples}


# 兼容旧调用名
run = run_backtest


def render(report: dict) -> str:
    samples = report["samples"]
    per_stock = report["per_stock"]
    lines: list[str] = []
    total = len(samples)
    if not total:
        return "无有效样本（请检查网络/数据源或扩大股票池）"

    base_up = sum(1 for s in samples if s["next_ret"] > 0) / total
    base_avg = statistics.mean(s["next_ret"] for s in samples)
    lines.append("=" * 62)
    lines.append("盘口分项离线回测报告（日线近似收盘时点）")
    lines.append("=" * 62)
    lines.append(f"股票池: {len(per_stock)} 只 | 有效样本: {total} 个")
    lines.append(f"基线: 次日上涨率 {base_up * 100:.1f}% | 次日平均涨跌 {base_avg:+.2f}%")
    lines.append("")

    # ---- 总分分桶：看评分方向是否单调
    lines.append("── 总分分桶 vs 次日表现 ──")
    buckets = [
        ("≥ +6", lambda s: s["score"] >= 6),
        ("+3~+5", lambda s: 3 <= s["score"] <= 5),
        ("+1~+2", lambda s: 1 <= s["score"] <= 2),
        ("0",     lambda s: s["score"] == 0),
        ("-1~-2", lambda s: -2 <= s["score"] <= -1),
        ("-3~-5", lambda s: -5 <= s["score"] <= -3),
        ("≤ -6",  lambda s: s["score"] <= -6),
    ]
    lines.append(f"{'分桶':<8}{'样本':>6}{'次日上涨率':>12}{'vs基线':>9}{'平均涨跌':>10}")
    for label, fn in buckets:
        sub = [s for s in samples if fn(s)]
        if not sub:
            continue
        up = sum(1 for s in sub if s["next_ret"] > 0) / len(sub)
        avg = statistics.mean(s["next_ret"] for s in sub)
        lines.append(f"{label:<8}{len(sub):>6}{up * 100:>11.1f}%{(up - base_up) * 100:>+8.1f}%{avg:>+9.2f}%")
    lines.append("")

    # ---- 各信号分组命中率
    lines.append("── 信号命中率（命中=看多信号次日涨 / 看空信号次日跌）──")
    lines.append(f"{'信号':<8}{'方向':<4}{'样本':>6}{'命中率':>9}{'次日涨率':>10}{'平均涨跌':>10}  校准建议")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        for label, bullish in s["labels"]:
            grouped[label].append({**s, "bullish": bullish})
    for label, _fn, bullish in SIGNAL_RULES:
        sub = grouped.get(label, [])
        n = len(sub)
        if not n:
            lines.append(f"{label:<8}{'看多' if bullish else '看空':<4}{0:>6}{'-':>9}{'-':>10}{'-':>10}  未触发")
            continue
        hit = sum(1 for s in sub if (s["next_ret"] > 0) == s["bullish"])
        hit_rate = hit / n
        up = sum(1 for s in sub if s["next_ret"] > 0) / n
        avg = statistics.mean(s["next_ret"] for s in sub)
        lines.append(
            f"{label:<8}{'看多' if bullish else '看空':<4}{n:>6}{hit_rate * 100:>8.1f}%"
            f"{up * 100:>9.1f}%{avg:>+9.2f}%  {calibrate(hit_rate, base_up, n)}"
        )
    lines.append("")
    lines.append("说明: 命中率以「信号方向×次日方向一致」计；基线=全样本次日上涨率。")
    lines.append("      ≥基线+5pct 有效可加码；<基线-3pct 建议降权或反向。")
    lines.append("      注意: 本回测以日线近似收盘时点（现价=收盘），盘中实时信号的")
    lines.append("      强弱可能更强/更弱；样本<50 的信号结论仅作参考。")
    return "\n".join(lines)


def selftest() -> int:
    """离线自测：用合成日线验证脚本逻辑（不触网），供 CI 冒烟引用。"""
    from backend.providers.base import Bar

    bars = []
    base = 10.0
    for i in range(1, 60):
        # 构造一段含涨跌/放量/高低位的序列：奇数日冲高回落，偶数日低位回升
        close = base + (1.0 if i % 2 == 0 else -0.8)
        high = close + 0.5
        low = close - 0.5
        vol = 2e7 if i % 3 == 0 else 1e7
        bars.append(
            Bar(date=f"2026-01-{i:02d}", open=close - 0.1, close=close, high=high, low=low,
                volume=vol, turnover=1.2, change_pct=(close - base) / base * 100)
        )
        base = close
    samples: list[dict] = []
    for i in range(6, len(bars) - 1):
        bar, prev = bars[i], bars[i - 1]
        avg5 = statistics.mean(b.volume for b in bars[i - 5:i])
        q = make_quote(bar, prev.close, avg5)
        score, note = analysis._intraday_score(q)
        nxt = bars[i + 1]
        samples.append({
            "score": score, "note": note,
            "next_ret": (nxt.close - bar.close) / bar.close * 100,
            "labels": signal_labels(note),
        })
    assert len(samples) > 40, "自测样本不足"
    assert all(isinstance(s["score"], int) and isinstance(s["next_ret"], float) for s in samples)
    print(f"盘口回测自测通过（{len(samples)} 个合成样本）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="盘口分项离线回测")
    parser.add_argument("--codes", default=",".join(DEFAULT_CODES), help="逗号分隔股票代码")
    parser.add_argument("--days", type=int, default=250, help="每只股票回测日线根数")
    parser.add_argument("--json", default="", help="额外输出机器可读报告到该文件")
    parser.add_argument("--selftest", action="store_true", help="离线自测（不触网）")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    codes = [normalize_code(c) for c in args.codes.split(",") if normalize_code(c)]
    print(f"拉取 {len(codes)} 只股票 × {args.days} 根日线（东财→同花顺→新浪故障转移）...")
    report = asyncio.run(run_backtest(codes, args.days, verbose=True))

    text = render(report)
    print()
    print(text)

    if args.json:
        out = {
            "codes": codes,
            "days": args.days,
            "baseline": {
                "total": len(report["samples"]),
                "up_rate": sum(1 for s in report["samples"] if s["next_ret"] > 0) / len(report["samples"])
                if report["samples"] else 0,
            },
            "samples": report["samples"],
            "per_stock": report["per_stock"],
        }
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n机器可读报告已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
