"""数据源健康自检工具：一条命令检查各数据源可用性、K 线最新日期、行情延迟。

用法（项目根目录）：
    python backend/check_sources.py            # 默认样本股
    python backend/check_sources.py --code 600000   # 指定单只股票
    python backend/check_sources.py --json     # 输出 JSON（脚本/监控用）

逐源逐能力实测（不经过注册表故障转移，能看到每个源的真实状态），
输出诊断报告：装配/行情/K线/资金流/两融/搜索/热门榜 + 数据新鲜度结论。
退出码：0=至少一个行情源可用；1=全部行情源不可用；2=用法错误。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import settings  # noqa: E402
from backend.providers import registry  # noqa: E402
from backend.providers.base import NotSupported, ProviderError, Throttled, close_client  # noqa: E402
from backend.utils import (  # noqa: E402
    data_is_stale,
    full_code,
    is_trading_now,
    kline_is_stale,
    now,
    session_state,
    today_str,
)

# 默认样本：覆盖 沪/深主板、创业板、科创板
SAMPLE: list[tuple[str, str]] = [
    ("600000", "SH"),  # 浦发银行
    ("000001", "SZ"),  # 平安银行
    ("300750", "SZ"),  # 宁德时代
    ("688981", "SH"),  # 中芯国际
]

TIMEOUT = 15.0


def _err_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:100]}"


async def _probe(coro: Any) -> tuple[bool, Any, str]:
    """执行一次探测，返回 (成功?, 结果, 摘要)。"""
    try:
        result = await asyncio.wait_for(coro, timeout=TIMEOUT)
        return True, result, ""
    except asyncio.TimeoutError:
        return False, None, "超时(>15s)"
    except Throttled as exc:
        return False, None, f"限流冷却: {str(exc)[:60]}"
    except NotSupported:
        return False, None, "无此能力"
    except ProviderError as exc:
        return False, None, _err_text(exc)
    except Exception as exc:  # noqa: BLE001
        return False, None, _err_text(exc)


def _fmt_price(v: Any) -> str:
    return "--" if v is None else f"{float(v):.2f}"


def _fmt_money(v: Any) -> str:
    if v is None:
        return "--"
    v = float(v)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}"


# ------------------------------------------------------------------ 各能力检查

async def check_quotes(p: Any, keys: list[tuple[str, str]]) -> dict[str, Any]:
    ok, result, err = await _probe(p.quotes(keys))
    if not ok:
        return {"ok": False, "error": err, "count": 0}
    quotes = result or {}
    names: dict[str, str] = {}
    prices: dict[str, float] = {}
    trade_dates: set[str] = set()
    for key, q in quotes.items():
        names[key] = q.name or "?"
        if q.price:
            prices[key] = q.price
        if q.trade_date:
            trade_dates.add(q.trade_date)
    return {
        "ok": True,
        "count": len(quotes),
        "names": names,
        "prices": prices,
        "trade_dates": sorted(trade_dates),
        "suspended": sum(1 for q in quotes.values() if q.status == "suspended"),
    }


async def check_kline(p: Any) -> dict[str, Any]:
    ok, result, err = await _probe(p.kline("600000", "SH", settings.KLINE_LIMIT))
    if not ok:
        return {"ok": False, "error": err}
    bars = result or []
    last = bars[-1].date if bars else ""
    return {
        "ok": True,
        "bars": len(bars),
        "last_date": last,
        "stale_flag": kline_is_stale(last) or data_is_stale(last),
        "first_date": bars[0].date if bars else "",
    }


async def check_flow(p: Any) -> dict[str, Any]:
    ok, result, err = await _probe(p.fund_flow("600000", "SH", 5))
    if not ok:
        return {"ok": False, "error": err}
    rows = result or []
    last = rows[-1].date if rows else ""
    return {
        "ok": True,
        "rows": len(rows),
        "last_date": last,
        "tiered": bool(rows and getattr(rows[-1], "lg", 0)),
        "last_main": _fmt_money(rows[-1].main) if rows else "--",
    }


async def check_margin(p: Any) -> dict[str, Any]:
    ok, result, err = await _probe(p.margin("600000", "SH", 5))
    if not ok:
        return {"ok": False, "error": err}
    rows = result or []
    last = rows[-1].date if rows else ""
    return {"ok": True, "rows": len(rows), "last_date": last}


async def check_search(p: Any) -> dict[str, Any]:
    ok, result, err = await _probe(p.search("浦发", 3))
    if not ok:
        return {"ok": False, "error": err}
    items = result or []
    return {"ok": True, "count": len(items), "first": items[0].name if items else ""}


async def check_hot(p: Any) -> dict[str, Any]:
    ok, result, err = await _probe(p.hot(3))
    if not ok:
        return {"ok": False, "error": err}
    data = result or {}
    counts = {k: len(v) for k, v in data.items()}
    return {"ok": True, "counts": counts}


async def check_news(p: Any) -> dict[str, Any]:
    ok, result, err = await _probe(p.news("600000", "SH", "浦发银行", 7, 5))
    if not ok:
        return {"ok": False, "error": err}
    items = result or []
    return {"ok": True, "count": len(items), "last_date": items[0].date[:10] if items else ""}


# ------------------------------------------------------------------ 盘口信号回测

async def check_backtest(sample: list[tuple[str, str | None]], days: int = 120) -> dict[str, Any]:
    """近期盘口信号命中率：用样本股的日线直接复用线上 _intraday_score。

    与独立回测脚本 backtest_intraday.py 同一套逻辑（该脚本支持完整股票池/自测），
    这里仅取样本股快速估算：总分分桶单调性 + 各信号命中率与校准建议。
    不触网失败时返回 ok=False，不影响整体自检。
    """
    from backtest_intraday import run_backtest, signal_labels
    import statistics

    codes = [c for c, _m in sample]
    try:
        res = await asyncio.wait_for(run_backtest(codes, days), timeout=TIMEOUT + 10)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"回测失败: {_err_text(exc)}"}
    samples = res.get("samples") or []
    if len(samples) < 30:
        return {"ok": False, "error": f"回测样本不足（{len(samples)} 个）", "samples": len(samples)}

    base_up = sum(1 for s in samples if s["next_ret"] > 0) / len(samples)
    base_avg = statistics.mean(s["next_ret"] for s in samples)

    # 总分分桶：看评分方向单调性
    buckets: list[dict[str, Any]] = []
    for label, fn in [
        ("≥+6",  lambda s: s["score"] >= 6),
        ("+3~+5", lambda s: 3 <= s["score"] <= 5),
        ("+1~+2", lambda s: 1 <= s["score"] <= 2),
        ("0",     lambda s: s["score"] == 0),
        ("-1~-2", lambda s: -2 <= s["score"] <= -1),
        ("-3~-5", lambda s: -5 <= s["score"] <= -3),
        ("≤-6",   lambda s: s["score"] <= -6),
    ]:
        sub = [s for s in samples if fn(s)]
        if not sub:
            continue
        up = sum(1 for s in sub if s["next_ret"] > 0) / len(sub)
        buckets.append({
            "bucket": label, "n": len(sub),
            "up_rate": round(up * 100, 1),
            "vs_base": round((up - base_up) * 100, 1),
            "avg_ret": round(statistics.mean(s["next_ret"] for s in sub), 2),
        })

    # 各信号命中率
    from backtest_intraday import SIGNAL_RULES
    grouped: dict[str, list[dict]] = {}
    for s in samples:
        for label, bullish in s.get("labels", []):
            grouped.setdefault(label, []).append({**s, "bullish": bullish})
    signals: list[dict[str, Any]] = []
    for label, _fn, bullish in SIGNAL_RULES:
        sub = grouped.get(label, [])
        n = len(sub)
        if not n:
            continue
        hit = sum(1 for s in sub if (s["next_ret"] > 0) == s["bullish"])
        hit_rate = hit / n
        up = sum(1 for s in sub if s["next_ret"] > 0) / n
        signals.append({
            "signal": label,
            "bullish": bullish,
            "n": n,
            "hit_rate": round(hit_rate * 100, 1),
            "up_rate": round(up * 100, 1),
            "avg_ret": round(statistics.mean(s["next_ret"] for s in sub), 2),
            "advice": _backtest_advice(hit_rate, base_up, n),
        })
    signals.sort(key=lambda x: (-x["hit_rate"], x["signal"]))

    return {
        "ok": True,
        "samples": len(samples),
        "stocks": len(res.get("per_stock") or []),
        "base_up_rate": round(base_up * 100, 1),
        "base_avg_ret": round(base_avg, 2),
        "buckets": buckets,
        "signals": signals,
    }


def _backtest_advice(hit_rate: float, base_rate: float, n: int) -> str:
    """与回测脚本同口径的校准建议。"""
    if n < 50:
        return "样本不足，暂不调整"
    delta = hit_rate - base_rate
    if delta >= 0.05:
        return "有效，可维持或上调权重"
    if delta >= 0.02:
        return "有效，权重可维持"
    if delta >= -0.03:
        return "偏弱，建议下调权重"
    return "反向/无效，建议大幅下调或检查方向"


# ------------------------------------------------------------------ 报告

def _cap_label(cap: str) -> str:
    return {
        "quotes": "行情", "kline": "K线", "fund_flow": "资金流", "margin": "两融",
        "search": "搜索", "hot": "热门榜", "boards": "板块", "industry": "行业",
        "news": "资讯",
    }.get(cap, cap)


def _status_line(ok: bool, detail: str) -> str:
    return f"  {'✅' if ok else '❌'} {detail}"


async def run_diagnostics(code: str | None) -> dict[str, Any]:
    reg = registry()
    sample: list[tuple[str, str | None]] = [(code, None)] if code else SAMPLE  # type: ignore[assignment]
    ts = now()
    report: dict[str, Any] = {
        "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "session": session_state(),
        "trading": is_trading_now(),
        "sample": [full_code(c, m) for c, m in sample],
        "providers": [],
        "issues": [],
    }

    caps_order = ["quotes", "kline", "fund_flow", "margin", "search", "hot", "news"]

    async def _check_one(p: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {"name": p.name, "caps": sorted(p.caps), "results": {}}
        for cap in caps_order:
            if cap not in p.caps:
                continue
            if cap == "quotes":
                entry["results"][cap] = await check_quotes(p, sample)
            elif cap == "kline":
                entry["results"][cap] = await check_kline(p)
            elif cap == "fund_flow":
                entry["results"][cap] = await check_flow(p)
            elif cap == "margin":
                entry["results"][cap] = await check_margin(p)
            elif cap == "search":
                entry["results"][cap] = await check_search(p)
            elif cap == "hot":
                entry["results"][cap] = await check_hot(p)
            elif cap == "news":
                entry["results"][cap] = await check_news(p)
        return entry

    # 各源并行探测（源间无共享限流，互不干扰），整体更快
    report["providers"] = list(await asyncio.gather(*(_check_one(p) for p in reg.providers)))

    # ---- 汇总：行情源可用数 / 各源 K 线新鲜度 ----
    quote_ok = [p for p in report["providers"] if p["results"].get("quotes", {}).get("ok")]
    report["quote_sources_ok"] = [p["name"] for p in quote_ok]

    # 行情延迟：取各源返回的最新 trade_date
    all_dates: set[str] = set()
    for p in report["providers"]:
        all_dates.update(p["results"].get("quotes", {}).get("trade_dates", []))
    report["latest_trade_date"] = max(all_dates) if all_dates else ""
    if report["latest_trade_date"]:
        if data_is_stale(report["latest_trade_date"]):
            report["issues"].append(
                f"行情日期 {report['latest_trade_date']} 距今超过 3 天，数据疑似陈旧"
            )

    # K 线新鲜度
    for p in report["providers"]:
        k = p["results"].get("kline")
        if k and k.get("ok") and k.get("stale_flag"):
            report["issues"].append(
                f"{p['name']} K线最后交易日 {k['last_date']}，缺少最新交易日（已判为延迟）"
            )

    # 资金流/两融滞后提示（两融 T+1 发布，资金流盘后应有当日）
    for p in report["providers"]:
        f = p["results"].get("fund_flow")
        if f and f.get("ok") and f.get("last_date") and f["last_date"] != report.get("latest_trade_date"):
            if not report["latest_trade_date"]:
                continue
            report["issues"].append(
                f"{p['name']} 资金流最后日期 {f['last_date']}（最新行情日 {report['latest_trade_date']}）"
            )

    # 行情源全部不可用
    if not quote_ok:
        report["issues"].append("全部行情源不可用，看板/详情将无法获取行情")

    # 盘口信号近期命中率（样本股快速回测；失败不阻塞自检）
    report["backtest"] = await check_backtest(sample)

    return report


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    a("=" * 62)
    a(f"数据源健康自检  {report['time']}  时段:{report['session']}"
      f"{'（盘中）' if report['trading'] else ''}")
    a(f"样本: {', '.join(report['sample'])}")
    a("=" * 62)

    for p in report["providers"]:
        a(f"\n[{p['name']}]  能力: {', '.join(_cap_label(c) for c in p['caps']) or '无'}")
        r = p["results"]

        q = r.get("quotes")
        if q is not None:
            if q["ok"]:
                names = q.get("names", {})
                prices = q.get("prices", {})
                sample_keys = list(report["sample"])
                sample_str = ", ".join(
                    f"{k} {names.get(k, '?')} {_fmt_price(prices.get(k))}" for k in sample_keys
                ) if len(sample_keys) <= 4 else ""
                extra = f" 停牌:{q['suspended']}" if q.get("suspended") else ""
                dates = ",".join(q.get("trade_dates", []))
                a(_status_line(True, f"行情 {q['count']}/{len(sample_keys)} 只"
                                   f"{' ' + sample_str if sample_str else ''}"
                                   f"  日期[{dates or '无'}]{extra}"))
            else:
                a(_status_line(False, f"行情: {q['error']}"))

        k = r.get("kline")
        if k is not None:
            if k["ok"]:
                flag = " ⚠️已判延迟" if k["stale_flag"] else ""
                a(_status_line(True, f"K线 {k['bars']} 根  {k['first_date']} ~ {k['last_date']}{flag}"))
            else:
                a(_status_line(False, f"K线: {k['error']}"))

        f = r.get("fund_flow")
        if f is not None:
            if f["ok"]:
                tier = "四档" if f.get("tiered") else "两档(简)"
                a(_status_line(True, f"资金流 {f['rows']} 日 最后:{f['last_date']} 主力:{f['last_main']} ({tier})"))
            else:
                a(_status_line(False, f"资金流: {f['error']}"))

        m = r.get("margin")
        if m is not None:
            if m["ok"]:
                a(_status_line(True, f"两融 {m['rows']} 日 最后:{m['last_date']}"))
            else:
                a(_status_line(False, f"两融: {m['error']}"))

        s = r.get("search")
        if s is not None:
            a(_status_line(s["ok"], f"搜索 {'✓' if s['ok'] else s['error']}"
                                    f"{(' 命中:' + s['first']) if s.get('ok') else ''}"))

        h = r.get("hot")
        if h is not None:
            if h["ok"]:
                a(_status_line(True, f"热门榜 {json.dumps(h['counts'], ensure_ascii=False)}"))
            else:
                a(_status_line(False, f"热门榜: {h['error']}"))

        n = r.get("news")
        if n is not None:
            a(_status_line(n["ok"], f"资讯 {'✓' if n['ok'] else n['error']}"
                                    f"{(' 条数:' + str(n['count']) + ' 最新:' + str(n['last_date'])) if n.get('ok') else ''}"))

    # ---- 盘口信号近期命中率
    bt = report.get("backtest")
    a("")
    a("-" * 62)
    a("盘口信号近期命中率（样本股快速回测）")
    if bt and bt.get("ok"):
        a(f"样本: {bt['samples']} 个 / {bt['stocks']} 只 | 基线次日上涨率 {bt['base_up_rate']}% "
          f"平均涨跌 {bt['base_avg_ret']:+.2f}%")
        a(f"{'分桶':<7}{'样本':>6}{'次日涨率':>10}{'vs基线':>9}{'平均涨跌':>9}")
        for b in bt["buckets"]:
            a(f"{b['bucket']:<7}{b['n']:>6}{b['up_rate']:>9.1f}%{b['vs_base']:>+8.1f}%{b['avg_ret']:>+8.2f}%")
        a(f"{'信号':<7}{'方向':<4}{'样本':>6}{'命中率':>9}{'次日涨率':>10}{'平均涨跌':>9}  校准建议")
        for s in bt["signals"]:
            a(f"{s['signal']:<7}{'看多' if s['bullish'] else '看空':<4}{s['n']:>6}"
              f"{s['hit_rate']:>8.1f}%{s['up_rate']:>9.1f}%{s['avg_ret']:>+8.2f}%  {s['advice']}")
        a("提示: 命中=看多信号次日涨/看空信号次日跌；日线近似收盘时点，样本<50 仅参考。")
    else:
        a(_status_line(False, f"盘口回测: {(bt or {}).get('error', '未执行')}"))

    a("")
    a("-" * 62)
    a(f"行情源可用: {len(report['quote_sources_ok'])} 个 "
      f"({', '.join(report['quote_sources_ok']) or '无'})")
    if report["latest_trade_date"]:
        a(f"最新行情日期: {report['latest_trade_date']}  (今天 {today_str()})")
    a("=" * 62)
    if report["issues"]:
        a(f"⚠️ 发现 {len(report['issues'])} 个问题:")
        for i, issue in enumerate(report["issues"], 1):
            a(f"  {i}. {issue}")
        a("提示: 行情源不足时系统会自动故障转移；标'已判延迟'的数据会显示'数据更新延迟'。")
    else:
        a("✅ 未发现问题：各数据源可用，行情/K线/资金数据均为最新。")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description="数据源健康自检")
    parser.add_argument("--code", help="指定单只股票代码（默认 4 只样本股）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    if args.code:
        if len(args.code) != 6 or not args.code.isdigit():
            print(f"用法错误: 股票代码应为 6 位数字，收到 {args.code!r}", file=sys.stderr)
            return 2

    try:
        report = await run_diagnostics(args.code)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(render_text(report))
        return 0 if report["quote_sources_ok"] else 1
    finally:
        await close_client()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
