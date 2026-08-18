"""盘中 vs 收盘时点对照实验：验证日线近似回测的校准结论在盘中场景是否成立。

背景：`_intraday_score` 生产环境是**盘中实时**信号（现价实时变化），但之前的
回测校准（backtest_intraday.py）用**日线近似收盘时点**（现价=收盘）。两者信号
强弱可能不同——尤其「高位强势/低位下跌」这类位置×方向信号，盘中触及高位与
收盘站在高位含义不同。本实验用东财 **5 分钟线**构造真实盘中快照（当日 14:00
时点的现价/累计高低/累计量），与同一交易日的收盘快照对比：

1. 信号方向差异：盘中与收盘时点打分方向翻转的比例
2. 命中率差异：各信号在两个时点下对次日涨跌的命中率
3. 校准回调建议：若盘中命中率与收盘显著不同，说明日线校准需要盘中回调

用法:
    python backtest_compare.py                          # 默认股票池
    python backtest_compare.py --codes 600000,601179
    python backtest_compare.py --json out.json          # 机器可读报告
    python backtest_compare.py --selftest               # 离线自测（不触网）

注意: 5 分钟线优先东方财富（单次 1024 根≈21 交易日），限流时自动回退
腾讯 mkline 翻页（每页 480 根=10 交易日，默认 3 页=30 天），两者均为
真实盘中数据；两源都失败时仅输出收盘时点结果并给出降级提示。
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

from backtest_intraday import DEFAULT_CODES, SIGNAL_RULES, make_quote, signal_labels  # noqa: E402

# 盘中时点：取每个交易日倒数第 12 根 5 分钟线（约 14:00，距收盘 1 小时）
INTRADAY_OFFSET = 12
# 腾讯 mkline 单次上限（480 根 = 10 个交易日 × 48 根）
MINUTE_LIMIT = 480
# 分钟线翻页数：每页 10 个交易日，3 页覆盖 30 天（与 --days 匹配）
MINUTE_PAGES = 3


async def _tencent_minutes(code: str, market: str, limit: int, pages: int = 1) -> list[Bar]:
    """腾讯 5 分钟线（ifzq.gtimg.cn mkline），支持翻页拉更长历史。

    翻页方式：首页取最近 limit 根，之后用最早一根的时间戳作为起点继续取，
    直到页数用尽或返回不足一页。字段与 Bar 对齐，量单位手->股。
    """
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
                break  # 已到历史尽头
    if not all_rows:
        raise RuntimeError("腾讯分钟线返回为空")
    all_rows.sort(key=lambda r: str(r[0]))
    bars: list[Bar] = []
    for row in all_rows:
        if len(row) < 6:
            continue
        dt = str(row[0])
        date = f"{dt[0:4]}-{dt[4:6]}-{dt[6:8]}"
        bars.append(
            Bar(
                date=f"{date} {dt[8:10]}:{dt[10:12]}",
                open=float(row[1]),
                close=float(row[2]),
                high=float(row[3]),
                low=float(row[4]),
                volume=float(row[5]) * 100,
            )
        )
    return bars


def _samples_for_day(
    code: str,
    day_bars: list[Bar],
    minute_bars: list[Bar],
    prev_close: float,
    avg_vol5: float,
) -> dict:
    """同一交易日构造 (收盘时点, 盘中时点) 两个快照。"""
    close_bar = day_bars[-1]
    day_close_q = make_quote(close_bar, prev_close, avg_vol5)
    close_score, close_note = analysis._intraday_score(day_close_q)

    intra_q: dict | None = None
    intra_score: int | None = None
    intra_note = ""
    if minute_bars and len(minute_bars) >= INTRADAY_OFFSET + 1:
        m = minute_bars[-INTRADAY_OFFSET]
        hi = max(b.high for b in minute_bars)
        lo = min(b.low for b in minute_bars)
        cum_vol = sum(b.volume for b in minute_bars)
        # 盘中量比：累计量 / 前5日均量（比收盘口径偏小，符合真实盘中量比特征）
        vr = (cum_vol / avg_vol5) if avg_vol5 else None
        # 盘中换手：按累计量占全天量的比例折算日线换手（直接用日线成交量）
        full_vol = close_bar.volume or 0
        turnover = None
        if close_bar.turnover and full_vol:
            turnover = close_bar.turnover * (cum_vol / full_vol)
        intra_q = {
            "price": m.close,
            "prev_close": prev_close,
            "high": hi,
            "low": lo,
            "change_pct": (m.close - prev_close) / prev_close * 100 if prev_close else None,
            "volume_ratio": vr,
            "turnover": turnover,
        }
        intra_score, intra_note = analysis._intraday_score(intra_q)

    return {
        "code": code,
        "date": close_bar.date,
        "close_score": close_score,
        "close_note": close_note,
        "close_labels": signal_labels(close_note),
        "intra_score": intra_score,
        "intra_note": intra_note,
        "intra_labels": signal_labels(intra_note) if intra_score is not None else [],
        "next_ret": 0.0,  # 由调用方回填（日线次日涨跌）
    }


async def run_compare(codes: list[str], days: int = 30, verbose: bool = False) -> dict:
    """拉日线 + 5 分钟线，逐交易日构造双时点快照。"""
    from backend.providers import registry as _reg

    samples: list[dict] = []
    per_stock: list[dict] = []
    for code in codes:
        market = resolve_market(code)
        em = next((p for p in _reg().providers if p.name == "eastmoney"), None)
        ths = next((p for p in _reg().providers if p.name == "ths"), None)
        # 日线：优先同花顺（避免消耗东财配额，5 分钟线更需要东财）
        day_bars_all: list[Bar] = []
        day_src = ""
        try:
            if ths is not None:
                day_bars_all = await ths.kline(code, market, days + 10)
                day_src = "ths"
        except Exception:  # noqa: BLE001
            day_bars_all = []
        if not day_bars_all:
            try:
                day_bars_all, day_src = await _reg().kline(code, market, days + 10)
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"  [{code}] 日线失败: {exc}")
                continue
        # 5 分钟线：东财优先（单次 1024 根≈21 交易日），失败回退腾讯翻页（30 交易日）
        minute_src = ""
        minute_bars_all: list[Bar] = []
        if em is not None:
            for attempt in range(3):
                try:
                    minute_bars_all = await em.kline(code, market, 1024, klt=5)
                    minute_src = "eastmoney"
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt < 2:
                        if verbose:
                            print(f"  [{code}] 东财5分钟线第{attempt + 1}次失败，等待冷却重试: {exc}")
                        await asyncio.sleep(7 * (attempt + 1))
                    else:
                        if verbose:
                            print(f"  [{code}] 东财5分钟线失败，回退腾讯: {exc}")
        if not minute_bars_all:
            try:
                minute_bars_all = await _tencent_minutes(code, market, MINUTE_LIMIT, MINUTE_PAGES)
                minute_src = "tencent"
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"  [{code}] 腾讯5分钟线也失败（降级仅收盘时点）: {exc}")

        # 按日期分组
        day_by_date: dict[str, list[Bar]] = defaultdict(list)
        for b in day_bars_all:
            day_by_date[b.date[:10]].append(b)
        minute_by_date: dict[str, list[Bar]] = defaultdict(list)
        for b in minute_bars_all:
            minute_by_date[b.date[:10]].append(b)

        dates = sorted(day_by_date)
        usable = 0
        for i in range(6, len(dates) - 1):
            d = dates[i]
            nxt = dates[i + 1]
            bars = day_by_date[d]
            if not bars:
                continue
            close_bar = bars[-1]
            prev_close = day_by_date[dates[i - 1]][-1].close
            avg_vol5 = statistics.mean(
                day_by_date[dates[j]][-1].volume for j in range(i - 5, i)
            )
            nxt_close = day_by_date[nxt][-1].close
            if not (prev_close and close_bar.close and nxt_close):
                continue
            s = _samples_for_day(
                code, bars, minute_by_date.get(d, []), prev_close, avg_vol5
            )
            s["next_ret"] = (nxt_close - close_bar.close) / close_bar.close * 100
            samples.append(s)
            usable += 1
        per_stock.append({
            "code": code, "day_source": day_src, "minute_source": minute_src,
            "usable": usable, "with_intraday": sum(1 for s in samples if s["intra_score"] is not None),
        })
        if verbose:
            print(f"  [{code}] 日线源={day_src} 分钟源={minute_src or '无'} 样本={usable}")

    return {"per_stock": per_stock, "samples": samples}


def _hit_rate(items: list[dict], bullish: bool) -> tuple[float, int]:
    if not items:
        return 0.0, 0
    hit = sum(1 for s in items if (s["next_ret"] > 0) == bullish)
    return hit / len(items), len(items)


def render(report: dict) -> str:
    samples = report["samples"]
    lines: list[str] = []
    total = len(samples)
    if not total:
        return "无有效样本（请检查网络/数据源）"
    with_intra = sum(1 for s in samples if s["intra_score"] is not None)
    base_up = sum(1 for s in samples if s["next_ret"] > 0) / total
    lines.append("=" * 70)
    lines.append("盘中 vs 收盘时点对照实验（真实 5 分钟线盘中快照）")
    lines.append("=" * 70)
    lines.append(f"样本: {total} 个（含盘中时点 {with_intra} 个）| 基线次日上涨率 {base_up * 100:.1f}%")
    lines.append("")

    # ---- 1) 信号方向差异：盘中与收盘打分方向翻转率
    lines.append("── 1. 打分方向一致性（收盘 vs 盘中 14:00）──")
    flip = same = 0
    dir_flip_by_label: dict[str, int] = defaultdict(int)
    dir_same_by_label: dict[str, int] = defaultdict(int)
    for s in samples:
        if s["intra_score"] is None:
            continue
        c, i = s["close_score"], s["intra_score"]
        cd = 1 if c > 0 else (-1 if c < 0 else 0)
        idd = 1 if i > 0 else (-1 if i < 0 else 0)
        if cd == 0 or idd == 0:
            continue  # 只看两时点都有明确方向的
        if cd == idd:
            same += 1
            for label, _b in s["close_labels"]:
                dir_same_by_label[label] += 1
        else:
            flip += 1
            for label, _b in s["close_labels"]:
                dir_flip_by_label[label] += 1
    total_dir = same + flip
    if total_dir:
        lines.append(f"两时点都有明确方向: {total_dir} 个，方向一致 {same}（{same / total_dir * 100:.1f}%），"
                     f"方向翻转 {flip}（{flip / total_dir * 100:.1f}%）")
        lines.append("  收盘信号在盘中时点翻转的信号（按收盘触发信号统计）：")
        for label, _fn, _b in SIGNAL_RULES:
            f = dir_flip_by_label.get(label, 0)
            t = f + dir_same_by_label.get(label, 0)
            if t >= 20:
                lines.append(f"    {label:<6} 翻转 {f}/{t}（{f / t * 100:.0f}%）")
    else:
        lines.append("  无两时点都有明确方向的样本（数据不足）")
    lines.append("")

    # ---- 2) 各信号命中率：收盘 vs 盘中
    lines.append("── 2. 信号命中率对比（命中=看多信号次日涨 / 看空信号次日跌）──")
    lines.append(f"{'信号':<8}{'方向':<4}{'收盘n':>6}{'收盘命中':>9}{'盘中n':>6}{'盘中命中':>9}{'差异':>8}  结论")
    close_group: dict[str, list[dict]] = defaultdict(list)
    intra_group: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        for label, bullish in s["close_labels"]:
            close_group[label].append({**s, "bullish": bullish})
        for label, bullish in s["intra_labels"]:
            intra_group[label].append({**s, "bullish": bullish})
    for label, _fn, bullish in SIGNAL_RULES:
        cg, ig = close_group.get(label, []), intra_group.get(label, [])
        if not cg and not ig:
            continue
        c_rate, c_n = _hit_rate(cg, bullish)
        i_rate, i_n = _hit_rate(ig, bullish)
        if c_n or i_n:
            diff = (i_rate - c_rate) * 100
            conclusion = ""
            if c_n >= 30 and i_n >= 30:
                if diff >= 5:
                    conclusion = "盘中更强，校准需回调"
                elif diff <= -5:
                    conclusion = "盘中更弱，校准可维持/加强"
                else:
                    conclusion = "差异不显著"
            elif i_n < 30:
                conclusion = "盘中样本不足"
            lines.append(
                f"{label:<8}{'看多' if bullish else '看空':<4}{c_n:>6}{c_rate * 100:>8.1f}%"
                f"{i_n:>6}{i_rate * 100:>8.1f}%{diff:>+7.1f}%  {conclusion}"
            )
    lines.append("")

    # ---- 3) 校准回调建议
    lines.append("── 3. 校准回调建议 ──")
    recalled: list[str] = []
    kept: list[str] = []
    for label, _fn, bullish in SIGNAL_RULES:
        cg, ig = close_group.get(label, []), intra_group.get(label, [])
        if len(cg) < 30 or len(ig) < 30:
            continue
        c_rate, _ = _hit_rate(cg, bullish)
        i_rate, _ = _hit_rate(ig, bullish)
        diff = i_rate - c_rate
        if diff >= 0.05:
            recalled.append(f"{label}（盘中命中率 {i_rate * 100:.0f}% 高于收盘 {c_rate * 100:.0f}%，"
                            f"日线校准低估了盘中信号，建议回调权重）")
        elif diff <= -0.05:
            kept.append(f"{label}（盘中 {i_rate * 100:.0f}% ≤ 收盘 {c_rate * 100:.0f}%，校准方向安全，可维持）")
        else:
            kept.append(f"{label}（盘中与收盘差异不显著，可维持）")
    if recalled:
        lines.append("需要盘中回调：")
        for x in recalled:
            lines.append(f"  ⚠ {x}")
    else:
        lines.append("  无需要回调的信号（或样本不足无法判断）")
    if kept:
        lines.append("可维持/已覆盖：")
        for x in kept[:6]:
            lines.append(f"  ✅ {x}")
    lines.append("")
    lines.append("说明: 盘中时点=当日 14:00 真实快照（5分钟线累计高低/量），命中率以")
    lines.append("「信号方向×次日方向一致」计；样本<30 结论仅参考。")
    return "\n".join(lines)


def selftest() -> int:
    """离线自测：合成日线 + 合成分钟线，验证脚本不抛错、结构完整。"""
    from backend.providers.base import Bar

    day_by_date: dict[str, list[Bar]] = defaultdict(list)
    minute_by_date: dict[str, list[Bar]] = defaultdict(list)
    base = 10.0
    for i in range(1, 25):
        d = f"2026-07-{i:02d}"
        close = base + (1.0 if i % 2 == 0 else -0.8)
        day_by_date[d] = [Bar(date=d, open=close - 0.1, close=close, high=close + 0.5, low=close - 0.5,
                              volume=2e7, turnover=1.2, change_pct=(close - base) / base * 100)]
        # 合成当日 48 根 5 分钟线（14:00 前后走势不同）
        mins = []
        for k in range(48):
            t = f"{d} {9 + k // 12:02d}:{30 + (k % 12) * 5:02d}" if k < 24 else f"{d} {13 + (k - 24) // 12:02d}:{5 + (k % 12) * 5:02d}"
            mins.append(Bar(date=t, open=close - 0.05, close=close + (0.05 if k < 36 else -0.03),
                            high=close + 0.1, low=close - 0.1, volume=4e5))
        minute_by_date[d] = mins
        base = close
    dates = sorted(day_by_date)
    samples: list[dict] = []
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
    text = render({"per_stock": [], "samples": samples})
    assert "盘中 vs 收盘时点对照实验" in text
    print(f"对照实验自测通过（{len(samples)} 个合成样本，渲染含盘中/收盘两时点）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="盘中 vs 收盘时点对照实验")
    parser.add_argument("--codes", default=",".join(DEFAULT_CODES), help="逗号分隔股票代码")
    parser.add_argument("--days", type=int, default=30, help="回测交易日数（分钟线按此拉取）")
    parser.add_argument("--json", default="", help="额外输出机器可读报告到该文件")
    parser.add_argument("--selftest", action="store_true", help="离线自测（不触网）")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    codes = [normalize_code(c) for c in args.codes.split(",") if normalize_code(c)]
    print(f"拉取 {len(codes)} 只股票：日线 {args.days} 天 + 5 分钟线（东财优先/腾讯翻页回退）...")
    report = asyncio.run(run_compare(codes, args.days, verbose=True))
    text = render(report)
    print()
    print(text)
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n机器可读报告已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
