"""同花顺数据源（辅）：日线 K 线 + 券商研报，来自同花顺行情网页加载的真实数据。

- K线：`d.10jqka.com.cn/v6/line/{sym}/01/last.js` 是同花顺行情网页图
  （stockpage.10jqka.com.cn）直接加载的日线数据文件（web 层，非 JSON API），
  含最新交易日、与网页展示一致。
- 研报：`basic.10jqka.com.cn/{code}/news.html` 内嵌券商研报 JSON（标题/机构/
  研究员/评级/日期/原文链接），与同花顺个股页展示一致。

同花顺实时行情接口带 hexin-v 反爬签名，容器内不稳定，故此处只实现日线文件接口。
东财 K 线不可用时优先回退到本源（而非腾讯/新浪接口），保证详情页数据真实可溯源。
"""
from __future__ import annotations

import json

from ..utils import normalize_code, resolve_market, to_float
from .base import Bar, Provider, ProviderError, ReportItem, fetch

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
        super().__init__(name="ths", caps={"kline", "reports"})

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

    # ------------------------------------------------------------ 券商研报
    async def reports(self, code: str, market: str, limit: int = 15) -> list[ReportItem]:
        """从同花顺个股页内嵌的研报 JSON 提取券商研报，按日期倒序。

        同花顺研报 JSON 结构：
        [{"thspj":"增持","title":"...","source":"国海证券","researcher":"林加力",
          "date":"2026-08-14","url":"http://news.10jqka.com.cn/field/sr/...shtml"}, ...]
        """
        resp = await fetch(
            f"https://basic.10jqka.com.cn/{normalize_code(code)}/news.html",
            headers=HEADERS,
        )
        text = _decode(resp.content)
        start = text.find('"url":')
        if start < 0:
            raise ProviderError("同花顺个股页未返回研报数据")
        lb = text.rfind("[", 0, start)
        if lb < 0:
            raise ProviderError("同花顺研报 JSON 定位失败")
        # 括号匹配找 JSON 数组结尾
        depth = 0
        end = -1
        in_str = False
        esc = False
        for i in range(lb, min(lb + 400000, len(text))):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        if end < 0:
            raise ProviderError("同花顺研报 JSON 未闭合")
        try:
            rows = json.loads(text[lb:end])
        except json.JSONDecodeError as exc:
            raise ProviderError("同花顺研报 JSON 解析失败") from exc
        if not isinstance(rows, list):
            raise ProviderError("同花顺研报数据格式异常")

        out: list[ReportItem] = []
        for row in rows:
            title = str(row.get("title") or "").strip()
            date = str(row.get("date") or "").strip()
            if not title or not date:
                continue
            out.append(
                ReportItem(
                    id=str(row.get("url") or "").rsplit("/", 1)[-1] or date,
                    date=date[:10],
                    source=str(row.get("source") or "").strip(),
                    researcher=str(row.get("researcher") or "").strip(),
                    rating=str(row.get("thspj") or "").strip(),
                    title=title,
                    url=str(row.get("url") or "").strip(),
                )
            )
        # 按日期倒序（新→旧）。返回全量（上限防滥用），由组装层负责截断展示与评级统计
        out.sort(key=lambda r: r.date, reverse=True)
        if not out:
            raise ProviderError("同花顺未返回研报")
        return out[:max(limit, 200)]
