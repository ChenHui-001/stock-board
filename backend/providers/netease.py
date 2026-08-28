"""网易财经数据源（备用）：实时行情 + 日线 K 线。

- 行情：`api.money.126.net/data/feed/` 支持批量，返回 JSONP，稳定但字段较少。
- K线：`quotes.money.163.com/service/chddata.html` 返回 CSV，覆盖沪深 A 股。

作为 PROVIDER_ORDER 末尾的兜底源，当东财/腾讯/新浪均不可用时提供基础行情与 K线。
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from typing import Any

from ..utils import TZ, normalize_code, resolve_market, to_float
from .base import Bar, Provider, ProviderError, Quote, SearchItem, fetch

QUOTE_URL = "https://api.money.126.net/data/feed/"
KLINE_URL = "https://quotes.money.163.com/service/chddata.html"
HEADERS = {"Referer": "https://quotes.money.163.com/"}


def _netease_symbol(code: str, market: str | None = None) -> str:
    """网易行情 symbol：沪市前缀 0，深市前缀 1。"""
    code = normalize_code(code)
    market = market or resolve_market(code)
    prefix = "0" if market == "SH" else "1"
    return f"{prefix}{code}"


class NeteaseProvider(Provider):
    def __init__(self) -> None:
        super().__init__(name="netease", caps={"quotes", "kline"})

    async def quotes(self, keys: list[tuple[str, str]]) -> dict[str, Quote]:
        if not keys:
            return {}
        symbols = ",".join(_netease_symbol(c, m) for c, m in keys)
        resp = await fetch(f"{QUOTE_URL}{symbols},money.api", headers=HEADERS)
        text = resp.text or ""
        # JSONP: _ntes_quote_callback({...})
        start, end = text.find("("), text.rfind(")")
        if start < 0 or end <= start:
            raise ProviderError("网易行情返回非 JSONP")
        try:
            payload = json.loads(text[start + 1 : end])
        except json.JSONDecodeError as exc:
            raise ProviderError("网易行情解析失败") from exc

        out: dict[str, Quote] = {}
        for code, market in keys:
            symbol = _netease_symbol(code, market)
            item = payload.get(symbol)
            if not item or not isinstance(item, dict):
                continue
            price = to_float(item.get("price"))
            prev = to_float(item.get("yestclose"))
            change_pct = to_float(item.get("percent"))
            quote = Quote(
                code=normalize_code(code),
                market=market or resolve_market(code),
                name=item.get("name", ""),
                price=price,
                prev_close=prev,
                open=to_float(item.get("open")),
                high=to_float(item.get("high")),
                low=to_float(item.get("low")),
                change=to_float(item.get("updown")),
                change_pct=change_pct,
                volume=to_float(item.get("volume")),
                amount=to_float(item.get("turnover")),
                trade_date=item.get("date", ""),
                source=self.name,
            )
            if not quote.price:
                quote.status, quote.status_text = "suspended", "股票停牌"
            out[f"{quote.code}.{quote.market}"] = quote
        if not out:
            raise ProviderError("网易行情返回为空")
        return out

    async def kline(self, code: str, market: str, limit: int) -> list[Bar]:
        symbol = _netease_symbol(code, market)
        end = datetime.now(TZ)
        start = end - timedelta(days=int(limit * 1.5) + 90)
        params = {
            "code": symbol,
            "start": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "fields": "TCLOSE;HIGH;LOW;TOPEN;LCLOSE;CHG;PCHG;VOTURNOVER;VATURNOVER",
        }
        resp = await fetch(KLINE_URL, headers=HEADERS, params=params)
        text = resp.text or ""
        reader = csv.reader(io.StringIO(text))
        bars: list[Bar] = []
        header = True
        for row in reader:
            if header:
                header = False
                continue
            if len(row) < 9:
                continue
            try:
                date = row[0]
                if len(date) != 10:
                    continue
                bars.append(
                    Bar(
                        date=date,
                        close=to_float(row[3], 0.0) or 0.0,
                        high=to_float(row[4], 0.0) or 0.0,
                        low=to_float(row[5], 0.0) or 0.0,
                        open=to_float(row[6], 0.0) or 0.0,
                        volume=to_float(row[8], 0.0) or 0.0,
                        change_pct=to_float(row[7]),
                    )
                )
            except (ValueError, IndexError):
                continue
        if not bars:
            raise ProviderError("网易K线返回为空")
        bars.sort(key=lambda b: b.date)
        return bars[-limit:]
