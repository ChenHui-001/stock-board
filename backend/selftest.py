"""数据源冒烟测试：python -m backend.selftest"""
from __future__ import annotations

import asyncio
import sys

from .providers import registry, shutdown
from .providers.base import ProviderError

CODES = [("600000", "SH"), ("000001", "SZ"), ("300750", "SZ")]


async def main() -> int:
    reg = registry()
    failed = 0

    print("已注册数据源:", [p.name for p in reg.providers])

    try:
        quotes, used = await reg.quotes(CODES)
        print(f"\n[行情] 源={used} 命中 {len(quotes)}/{len(CODES)}")
        for key, q in quotes.items():
            print(f"  {key} {q.name:<8} 现价={q.price} 昨收={q.prev_close} "
                  f"涨跌幅={q.change_pct}% 板块={q.board} 源={q.source}")
    except ProviderError as exc:
        failed += 1
        print("[行情] 失败:", exc)

    try:
        items = await reg.search("pfyh", 5)
        print(f"\n[搜索 pfyh] {[(i.code, i.name, i.market) for i in items]}")
        items2 = await reg.search("宁德", 5)
        print(f"[搜索 宁德] {[(i.code, i.name, i.market) for i in items2]}")
    except ProviderError as exc:
        failed += 1
        print("[搜索] 失败:", exc)

    try:
        bars, src = await reg.kline("600000", "SH", 70)
        print(f"\n[K线] 源={src} 条数={len(bars)} 最后一根={bars[-1]}")
    except ProviderError as exc:
        failed += 1
        print("[K线] 失败:", exc)

    try:
        flow, src = await reg.fund_flow("600000", "SH", 30)
        print(f"\n[资金流向] 源={src} 条数={len(flow)} 最后一天={flow[-1] if flow else None}")
    except ProviderError as exc:
        failed += 1
        print("[资金流向] 失败:", exc)

    try:
        margin, src = await reg.margin("600000", "SH", 30)
        print(f"\n[两融] 源={src} 条数={len(margin)} 最后一天={margin[-1] if margin else None}")
    except ProviderError as exc:
        failed += 1
        print("[两融] 失败:", exc)

    try:
        boards = await reg.boards("600000", "SH")
        print(f"\n[板块] {boards[:8]}")
    except ProviderError as exc:
        failed += 1
        print("[板块] 失败:", exc)

    try:
        hot = await reg.hot(3)
        print(f"\n[热门] 涨幅={[ (q.name, q.change_pct) for q in hot['gainers'] ]}")
        print(f"       成交={[ (q.name, q.amount) for q in hot['actives'] ]}")
    except ProviderError as exc:
        failed += 1
        print("[热门] 失败:", exc)

    # 单源直测：验证兜底源本身可用
    print("\n--- 兜底源直测 ---")
    for provider in reg.providers:
        if "quotes" not in provider.caps or provider.name == "eastmoney":
            continue
        try:
            data = await provider.quotes([("600000", "SH")])
            q = data.get("600000.SH")
            print(f"  {provider.name}: {q.name} {q.price} {q.change_pct}%")
        except Exception as exc:  # noqa: BLE001
            print(f"  {provider.name}: 失败 {exc}")
    for provider in reg.providers:
        if "kline" not in provider.caps or provider.name == "eastmoney":
            continue
        try:
            bars = await provider.kline("600000", "SH", 5)
            print(f"  {provider.name} K线: {len(bars)} 根，最后 {bars[-1].date} 收 {bars[-1].close}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {provider.name} K线: 失败 {exc}")

    await shutdown()
    print("\n失败项:", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
