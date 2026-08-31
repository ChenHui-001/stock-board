"""命令行入口：python -m backend.backtest

    python -m backend.backtest --list
    python -m backend.backtest --strategy score_threshold --limit 400
    python -m backend.backtest --strategy intraday_signal --selftest
    python -m backend.backtest --strategy intraday_compare --selftest
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from . import registry as _registry
from . import score_strategy, intraday_strategy, compare_strategy


def _print_result(result: dict[str, Any]) -> None:
    summary = result.get("summary") or {}
    print("\n" + "=" * 62)
    print("回测完成")
    print("=" * 62)
    for k, v in summary.items():
        print(f"  {k}: {v}")
    tables = result.get("tables") or {}
    for name, rows in tables.items():
        if not rows or not isinstance(rows, list):
            continue
        print(f"\n── {name} ──")
        cols = [c for c in rows[0].keys() if not c.startswith("_")]
        print("  " + " | ".join(str(c) for c in cols))
        for r in rows[:20]:
            print("  " + " | ".join(str(r.get(c, "")) for c in cols))


async def _amain() -> int:
    ap = argparse.ArgumentParser(description="股票看板 · 策略回测模块")
    ap.add_argument("--list", action="store_true", help="列出所有策略与参数")
    ap.add_argument("--strategy", default="", help="策略 id")
    ap.add_argument("--selftest", action="store_true", help="离线自测（不触网）")
    ap.add_argument("--codes", default="", help="覆盖股票池（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="覆盖日线根数（score 策略）")
    ap.add_argument("--days", type=int, default=0, help="覆盖日线根数（intraday / compare）")
    args = ap.parse_args()

    if args.list or not args.strategy:
        for s in _registry.STRATEGIES:
            print(f"\n[{s.id}] {s.name}  ({s.kind})")
            print(f"  {s.desc}")
            if s.limits:
                print(f"  口径限制: {s.limits}")
            for f in s.schema:
                print(f"    - {f['key']}: {f['label']} = {f['default']}")
        return 0

    strat = _registry.get(args.strategy)
    if strat is None:
        print(f"未知策略: {args.strategy}", file=sys.stderr)
        return 2

    if args.selftest:
        if args.strategy == intraday_strategy.STRATEGY_ID:
            return intraday_strategy.selftest()
        if args.strategy == compare_strategy.STRATEGY_ID:
            return compare_strategy.selftest()
        print(f"{args.strategy} 暂不支持离线自测", file=sys.stderr)
        return 2

    params: dict[str, Any] = {f["key"]: f["default"] for f in strat.schema}
    if args.codes:
        params["codes"] = args.codes
    if args.limit:
        params["limit"] = args.limit
    if args.days:
        params["days"] = args.days

    def on_progress(pct: float, stage: str) -> None:
        bar = "#" * int(pct * 30)
        print(f"\r  [{bar:<30}] {pct * 100:5.1f}%  {stage}", end="", flush=True)

    print(f"策略: {strat.name} ({strat.id})")
    print(f"参数: {json.dumps(params, ensure_ascii=False)}")
    result = await strat.runner(params, on_progress)
    print()
    _print_result(result)
    if result.get("report_html"):
        print(f"\n看板: {result['report_html']}")
    if result.get("trades_csv"):
        print(f"明细: {result['trades_csv']}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
