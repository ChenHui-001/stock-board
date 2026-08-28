"""腾讯自选股数据源（辅）：实时行情 + 代码联想搜索。返回 GBK 文本。"""
from __future__ import annotations

import json
import re

from datetime import datetime, timedelta

from ..utils import TZ, normalize_code, resolve_market, tencent_code, to_float
from .base import Bar, Provider, ProviderError, Quote, SearchItem, fetch

QUOTE_URL = "https://qt.gtimg.cn/q="
SUGGEST_URL = "https://smartbox.gtimg.cn/s3/"
HEADERS = {"Referer": "https://gu.qq.com/"}

_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescape(text: str) -> str:
    """联想接口按拼音查询时会把中文名返回成 \\uXXXX 字面量。"""
    if "\\u" not in text:
        return text
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _decode(raw: bytes) -> str:
    for enc in ("gbk", "gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


class TencentProvider(Provider):
    def __init__(self) -> None:
        super().__init__(name="tencent", caps={"quotes", "search", "kline", "kline_min"})

    async def quotes(self, keys: list[tuple[str, str]]) -> dict[str, Quote]:
        if not keys:
            return {}
        symbols = ",".join(tencent_code(c, m) for c, m in keys)
        resp = await fetch(QUOTE_URL + symbols, headers=HEADERS)
        text = _decode(resp.content)
        out: dict[str, Quote] = {}
        for line in text.split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            body = line.split("=", 1)[1].strip().strip('"')
            f = body.split("~")
            if len(f) < 40:
                continue
            code = normalize_code(f[2])
            if not code:
                continue
            market = resolve_market(code)
            price = to_float(f[3])
            prev = to_float(f[4])
            trade_date = ""
            if len(f) > 30 and len(f[30]) >= 8:
                raw = f[30]
                trade_date = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
            quote = Quote(
                code=code,
                market=market,
                name=f[1],
                price=price,
                prev_close=prev,
                open=to_float(f[5]),
                change=to_float(f[31]),
                change_pct=to_float(f[32]),
                high=to_float(f[33]),
                low=to_float(f[34]),
                volume=(to_float(f[6], 0.0) or 0.0) * 100,
                amount=(to_float(f[37], 0.0) or 0.0) * 10000,  # 万元 -> 元
                turnover=to_float(f[38]),
                volume_ratio=to_float(f[49]) if len(f) > 49 else None,
                trade_date=trade_date,
                source=self.name,
            )
            if not quote.price:
                quote.status, quote.status_text = "suspended", "股票停牌"
            out[f"{code}.{market}"] = quote
        if not out:
            raise ProviderError("腾讯行情返回为空")
        return out

    async def search(self, keyword: str, limit: int = 15) -> list[SearchItem]:
        resp = await fetch(SUGGEST_URL, headers=HEADERS, params={"q": keyword, "t": "all"})
        text = _decode(resp.content)
        if "=" not in text:
            return []
        body = text.split("=", 1)[1].strip().strip('";')
        items: list[SearchItem] = []
        for chunk in body.split("^"):
            parts = chunk.split("~")
            if len(parts) < 3:
                continue
            prefix, code, name = parts[0].lower(), normalize_code(parts[1]), _unescape(parts[2])
            if prefix not in ("sh", "sz", "bj") or len(code) != 6 or not code.isdigit():
                continue
            items.append(
                SearchItem(code=code, market=prefix.upper(), name=name, type="A股")
            )
            if len(items) >= limit:
                break
        return items

    # ------------------------------------------------------------ K 线
    async def kline(self, code: str, market: str, limit: int) -> list[Bar]:
        """腾讯日线（前复权），作为东财/同花顺不可用时的兜底。"""
        symbol = tencent_code(code, market)
        end = datetime.now(TZ)
        start = end - timedelta(days=int(limit * 1.5) + 60)
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={symbol},day,{start.strftime('%Y-%m-%d')},{end.strftime('%Y-%m-%d')},{limit},qfq"
        )
        resp = await fetch(url, headers=HEADERS)
        payload = json.loads(resp.text or "{}")
        data = payload.get("data", {}).get(symbol, {})
        rows = data.get("day") or []
        if not rows:
            raise ProviderError("腾讯K线返回为空")
        bars: list[Bar] = []
        for row in rows:
            if len(row) < 5:
                continue
            bars.append(
                Bar(
                    date=str(row[0]),
                    open=to_float(row[1], 0.0) or 0.0,
                    close=to_float(row[2], 0.0) or 0.0,
                    high=to_float(row[3], 0.0) or 0.0,
                    low=to_float(row[4], 0.0) or 0.0,
                    volume=to_float(row[5], 0.0) or 0.0 if len(row) > 5 else 0.0,
                )
            )
        if not bars:
            raise ProviderError("腾讯K线解析为空")
        # 补涨跌幅
        for i in range(1, len(bars)):
            prev = bars[i - 1].close
            if prev:
                bars[i].change_pct = round((bars[i].close - prev) / prev * 100, 2)
        return bars[-limit:]

    async def kline_min(self, code: str, market: str, limit: int, klt: int = 60) -> list[Bar]:
        """腾讯分钟 K 线（m1/m5/m15/m30/m60），默认 60 分钟。"""
        symbol = tencent_code(code, market)
        period = f"m{klt}"
        url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={symbol},{period},,{limit}"
        resp = await fetch(url, headers=HEADERS)
        payload = json.loads(resp.text or "{}")
        data = payload.get("data", {}).get(symbol, {})
        rows = data.get(period) or []
        if not rows:
            raise ProviderError("腾讯分钟K线返回为空")
        bars: list[Bar] = []
        for row in rows:
            if len(row) < 5:
                continue
            bars.append(
                Bar(
                    date=str(row[0]),
                    open=to_float(row[1], 0.0) or 0.0,
                    close=to_float(row[2], 0.0) or 0.0,
                    high=to_float(row[3], 0.0) or 0.0,
                    low=to_float(row[4], 0.0) or 0.0,
                    volume=to_float(row[5], 0.0) or 0.0 if len(row) > 5 else 0.0,
                )
            )
        return bars[-limit:]
