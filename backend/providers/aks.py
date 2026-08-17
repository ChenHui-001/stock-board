"""AkShare 数据源（可选辅助）。

默认不打进镜像（依赖 pandas，体积大且为同步阻塞调用）。
启用：构建时 `--build-arg WITH_AKSHARE=true`，运行时 `ENABLE_AKSHARE=true`。
所有调用放到线程池，避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from ..utils import normalize_code, now, resolve_market, to_float
from .base import Bar, FlowDay, Provider, ProviderError

log = logging.getLogger("providers.akshare")

_ak: Any = None
_import_failed = False


def _load() -> Any:
    global _ak, _import_failed
    if _ak is not None:
        return _ak
    if _import_failed:
        raise ProviderError("akshare 未安装")
    try:
        import akshare as ak  # type: ignore

        _ak = ak
        return _ak
    except Exception as exc:  # noqa: BLE001
        _import_failed = True
        log.warning("akshare 不可用：%s", exc)
        raise ProviderError("akshare 未安装") from exc


async def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"akshare 调用失败: {exc}") from exc


def _start_date(limit: int) -> str:
    """K线起始日期：按「日历日 ≈ 交易日 ×1.4 + 假期缓冲」折算，避免每次拉全量历史。"""
    days = int(limit * 1.4) + 30
    return (now().date() - timedelta(days=days)).strftime("%Y%m%d")


class AkshareProvider(Provider):
    def __init__(self) -> None:
        super().__init__(name="akshare", caps={"kline", "fund_flow"})

    async def kline(self, code: str, market: str, limit: int) -> list[Bar]:
        ak = _load()
        df = await _run(
            ak.stock_zh_a_hist,
            symbol=normalize_code(code),
            period="daily",
            start_date=_start_date(limit),
            end_date=now().strftime("%Y%m%d"),
            adjust="qfq",
        )
        if df is None or df.empty:
            raise ProviderError("akshare 未返回K线")
        bars = [
            Bar(
                date=str(r["日期"])[:10],
                open=to_float(r["开盘"], 0.0) or 0.0,
                close=to_float(r["收盘"], 0.0) or 0.0,
                high=to_float(r["最高"], 0.0) or 0.0,
                low=to_float(r["最低"], 0.0) or 0.0,
                volume=(to_float(r.get("成交量"), 0.0) or 0.0) * 100,
                amount=to_float(r.get("成交额"), 0.0) or 0.0,
                change_pct=to_float(r.get("涨跌幅")),
                turnover=to_float(r.get("换手率")),
            )
            for _, r in df.iterrows()
        ]
        return bars[-limit:]

    async def fund_flow(self, code: str, market: str, days: int) -> list[FlowDay]:
        ak = _load()
        df = await _run(
            ak.stock_individual_fund_flow,
            stock=normalize_code(code),
            market=(market or resolve_market(code)).lower(),
        )
        if df is None or df.empty:
            raise ProviderError("akshare 未返回资金流向")
        rows = [
            FlowDay(
                date=str(r["日期"])[:10],
                main=to_float(r.get("主力净流入-净额"), 0.0) or 0.0,
                sm=to_float(r.get("小单净流入-净额"), 0.0) or 0.0,
                md=to_float(r.get("中单净流入-净额"), 0.0) or 0.0,
                lg=to_float(r.get("大单净流入-净额"), 0.0) or 0.0,
                xl=to_float(r.get("超大单净流入-净额"), 0.0) or 0.0,
                main_pct=to_float(r.get("主力净流入-净占比")),
                close=to_float(r.get("收盘价")),
                change_pct=to_float(r.get("涨跌幅")),
            )
            for _, r in df.iterrows()
        ]
        return rows[-days:]
