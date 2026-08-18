"""同花顺数据源（辅）：日线 K 线，来自同花顺行情网页加载的真实数据文件。

`d.10jqka.com.cn/v6/line/{sym}/01/last.js` 是同花顺网页行情图（stockpage.10jqka.com.cn）
直接加载的日线数据文件（web 层，非 JSON API），含最新交易日、与网页展示一致。
同花顺实时行情接口带 hexin-v 反爬签名，容器内不稳定，故此处只实现日线文件接口。
东财 K 线不可用时优先回退到本源（而非腾讯/新浪接口），保证详情页数据真实可溯源。
"""
from __future__ import annotations

import json

from ..utils import normalize_code, resolve_market, to_float
from .base import Bar, Provider, ProviderError, fetch

KLINE_URL = "https://d.10jqka.com.cn/v6/line/{sym}/01/last.js"
HEADERS = {"Referer": "http://stockpage.10jqka.com.cn/"}


def _decode(raw: bytes) -> str:
    for enc in ("gbk", "gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _ths_symbol(code: str, market: str | None = None) -> str:
    code = normalize_code(code)
    market = market or resolve_market(code)
    return f"{'bj' if market == 'BJ' else 'hs'}_{code}"


class ThsProvider(Provider):
    def __init__(self) -> None:
        super().__init__(name="ths", caps={"kline"})

    async def kline(self, code: str, market: str, limit: int) -> list[Bar]:
        resp = await fetch(KLINE_URL.format(sym=_ths_symbol(code, market)), headers=HEADERS)
        text = _decode(resp.content)
        try:
            payload = json.loads(text[text.index("(") + 1 : text.rindex(")")])
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("同花顺K线解析失败") from exc

        raw = payload.get("data") or ""
        bars: list[Bar] = []
        for chunk in raw.split(";"):
            p = chunk.split(",")
            if len(p) < 7 or len(p[0]) != 8:
                continue
            d = p[0]
            bars.append(
                Bar(
                    date=f"{d[0:4]}-{d[4:6]}-{d[6:8]}",
                    open=to_float(p[1], 0.0) or 0.0,
                    high=to_float(p[2], 0.0) or 0.0,
                    low=to_float(p[3], 0.0) or 0.0,
                    close=to_float(p[4], 0.0) or 0.0,
                    volume=to_float(p[5], 0.0) or 0.0,
                    amount=to_float(p[6], 0.0) or 0.0,
                    turnover=to_float(p[7]) if len(p) > 7 else None,
                )
            )
        if not bars:
            raise ProviderError("同花顺未返回K线")
        # 补涨跌幅
        for i in range(1, len(bars)):
            prev = bars[i - 1].close
            if prev:
                bars[i].change_pct = round((bars[i].close - prev) / prev * 100, 2)
        return bars[-limit:]
