"""腾讯自选股数据源（辅）：实时行情 + 代码联想搜索。返回 GBK 文本。"""
from __future__ import annotations

import re

from ..utils import normalize_code, resolve_market, tencent_code, to_float
from .base import Provider, ProviderError, Quote, SearchItem, fetch

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
        super().__init__(name="tencent", caps={"quotes", "search"})

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
