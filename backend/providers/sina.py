"""新浪财经数据源（辅）：实时行情 + 日线 K 线 + 资金流向 + 个股新闻兜底。必须带 Referer，否则 403。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ..utils import TZ, normalize_code, resolve_market, sina_code, to_float
from .base import Bar, FlowDay, NewsItem, Provider, ProviderError, Quote, SearchItem, fetch

QUOTE_URL = "https://hq.sinajs.cn/list="
KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
)
FLOW_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
RANK_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
HEADERS = {"Referer": "https://finance.sina.com.cn"}


def _decode(raw: bytes) -> str:
    for enc in ("gbk", "gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


class SinaProvider(Provider):
    def __init__(self) -> None:
        super().__init__(name="sina", caps={"quotes", "search", "kline", "fund_flow", "hot", "news"})

    async def quotes(self, keys: list[tuple[str, str]]) -> dict[str, Quote]:
        if not keys:
            return {}
        index = {sina_code(c, m): (normalize_code(c), m) for c, m in keys}
        resp = await fetch(QUOTE_URL + ",".join(index), headers=HEADERS)
        text = _decode(resp.content)
        out: dict[str, Quote] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("var hq_str_") or "=" not in line:
                continue
            head, _, body = line.partition("=")
            symbol = head.replace("var hq_str_", "").strip()
            values = body.strip().strip(';"').split(",")
            if len(values) < 32 or symbol not in index:
                continue
            code, market = index[symbol]
            market = market or resolve_market(code)
            price = to_float(values[3])
            prev = to_float(values[2])
            change_pct = None
            if price and prev:
                change_pct = round((price - prev) / prev * 100, 2)
            quote = Quote(
                code=code,
                market=market,
                name=values[0],
                price=price,
                prev_close=prev,
                open=to_float(values[1]),
                change=round(price - prev, 4) if (price and prev) else None,
                change_pct=change_pct,
                high=to_float(values[4]),
                low=to_float(values[5]),
                volume=to_float(values[8]),
                amount=to_float(values[9]),
                trade_date=values[30] if len(values) > 30 else "",
                source=self.name,
            )
            if not quote.price:
                quote.status, quote.status_text = "suspended", "股票停牌"
            out[f"{code}.{market}"] = quote
        if not out:
            raise ProviderError("新浪行情返回为空")
        return out

    # ------------------------------------------------------------ 代码搜索
    async def search(self, keyword: str, limit: int = 15) -> list[SearchItem]:
        """新浪搜索联想（suggest3）：按名称/拼音/代码返回 A 股列表。"""
        if not keyword:
            return []
        resp = await fetch(
            "https://suggest3.sinajs.cn/suggest/type=11,12&key=" + keyword,
            headers=HEADERS,
        )
        text = _decode(resp.content)
        # 返回形如：suggestvalue="..."
        if "=" not in text:
            return []
        body = text.split("=", 1)[1].strip().strip('";')
        items: list[SearchItem] = []
        for chunk in body.split(";"):
            parts = chunk.split(",")
            # 实测格式：名称,市场类型(11=SH/12=SZ),代码,完整代码(sh600000),名称,...
            if len(parts) < 4:
                continue
            name, mtype, code = parts[0], parts[1], normalize_code(parts[2])
            if len(code) != 6 or not code.isdigit():
                continue
            if mtype == "11":
                market = "SH"
            elif mtype == "12":
                market = "SZ"
            else:
                market = resolve_market(code)
            items.append(SearchItem(code=code, market=market, name=name, type="A股"))
            if len(items) >= limit:
                break
        return items

    # ------------------------------------------------------------ 日线 K 线
    async def kline(self, code: str, market: str, limit: int) -> list[Bar]:
        """新浪日线（含当日，收盘后即有），作为东财 K 线不可用时的兜底。

        该接口返回未复权数据，除权日会有跳空，仅作兜底使用；
        东财（前复权）优先，新浪失败再退同花顺。
        """
        resp = await fetch(
            KLINE_URL,
            headers=HEADERS,
            params={"symbol": sina_code(code, market), "scale": 240, "ma": "no", "datalen": limit},
        )
        try:
            rows = json.loads(_decode(resp.content) or "[]")
        except json.JSONDecodeError as exc:
            raise ProviderError("新浪K线解析失败") from exc
        if not isinstance(rows, list) or not rows:
            raise ProviderError("新浪未返回K线")

        bars: list[Bar] = []
        for row in rows:
            try:
                date = str(row["day"] or "")[:10]
                if len(date) != 10:
                    continue
                bars.append(
                    Bar(
                        date=date,
                        open=to_float(row.get("open"), 0.0) or 0.0,
                        close=to_float(row.get("close"), 0.0) or 0.0,
                        high=to_float(row.get("high"), 0.0) or 0.0,
                        low=to_float(row.get("low"), 0.0) or 0.0,
                        volume=to_float(row.get("volume"), 0.0) or 0.0,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if not bars:
            raise ProviderError("新浪未返回K线")
        # 补涨跌幅（新浪该接口不含 change_pct）
        for i in range(1, len(bars)):
            prev = bars[i - 1].close
            if prev:
                bars[i].change_pct = round((bars[i].close - prev) / prev * 100, 2)
        return bars[-limit:]

    # ------------------------------------------------------------ 个股新闻（网页层）
    async def news(
        self, code: str, market: str, name: str, days: int = 30, limit: int = 15
    ) -> list[NewsItem]:
        """新浪个股新闻页（vCB_AllNewsStock）抓取：纯网页 HTML，含最新交易日新闻。

        东财搜索接口不可用时兜底；页面按时间倒序，过滤最近 days 天。
        """
        resp = await fetch(
            f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{sina_code(code, market)}.phtml",
            headers={"Referer": "https://finance.sina.com.cn"},
        )
        text = _decode(resp.content)
        items = re.findall(
            r"(\d{4}-\d{2}-\d{2})&nbsp;(\d{2}:\d{2})&nbsp;&nbsp;<a[^>]*href='([^']+)'[^>]*>(.*?)</a>",
            text,
        )
        today = datetime.now(TZ).date()
        out: list[NewsItem] = []
        for date, ttime, url, title in items:
            try:
                day = datetime.strptime(date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if (today - day).days > days:
                continue
            clean = re.sub(r"<[^>]+>", "", title).strip()
            out.append(
                NewsItem(
                    id=url.rsplit("/", 1)[-1] or url,
                    date=f"{date} {ttime}:00",
                    source="新浪财经",
                    title=clean,
                    summary=clean,
                    url=url.strip(),
                )
            )
            if len(out) >= limit:
                break
        if not out:
            raise ProviderError("新浪未返回该股票的近期资讯")
        return out

    async def fund_flow(self, code: str, market: str, days: int) -> list[FlowDay]:
        """兜底口径：新浪只提供「净流入总额」与「超大单净额」，
        没有大单/中单/小单四档拆分，其余档位以 0 返回并由前端标注数据源。
        """
        resp = await fetch(
            FLOW_URL,
            headers=HEADERS,
            params={
                "daima": sina_code(code, market),
                "num": days,
                "sort": "opendate",
                "asc": 0,
                "bankuai": "",
                "shichang": "",
            },
        )
        try:
            rows = json.loads(_decode(resp.content) or "[]")
        except json.JSONDecodeError as exc:
            raise ProviderError("新浪资金流向解析失败") from exc
        if not isinstance(rows, list) or not rows:
            raise ProviderError("新浪未返回资金流向")

        out: list[FlowDay] = []
        for row in rows:
            net = to_float(row.get("netamount"), 0.0) or 0.0
            xl = to_float(row.get("r0_net"), 0.0) or 0.0
            ratio = to_float(row.get("ratioamount"))
            out.append(
                FlowDay(
                    date=str(row.get("opendate") or "")[:10],
                    main=net,
                    xl=xl,
                    lg=0.0,
                    md=0.0,
                    sm=round(net - xl, 2),
                    main_pct=round(ratio * 100, 2) if ratio is not None else None,
                    close=to_float(row.get("trade")),
                    change_pct=(
                        round((to_float(row.get("changeratio")) or 0) * 100, 2)
                        if row.get("changeratio") is not None else None
                    ),
                )
            )
        out.reverse()  # 新浪按日期倒序返回，统一成正序
        return out[-days:]

    async def hot(self, limit: int = 10) -> dict[str, list[Quote]]:
        async def rank(sort: str, asc: int) -> list[Quote]:
            resp = await fetch(
                RANK_URL,
                headers=HEADERS,
                params={
                    "page": 1, "num": limit, "sort": sort, "asc": asc,
                    "node": "hs_a", "symbol": "",
                },
            )
            try:
                rows = json.loads(_decode(resp.content) or "[]")
            except json.JSONDecodeError as exc:
                raise ProviderError("新浪排行榜解析失败") from exc
            out: list[Quote] = []
            for row in rows or []:
                code = normalize_code(str(row.get("code") or ""))
                if not code:
                    continue
                symbol = str(row.get("symbol") or "")[:2].upper()
                market = symbol if symbol in ("SH", "SZ", "BJ") else resolve_market(code)
                out.append(
                    Quote(
                        code=code,
                        market=market,
                        name=str(row.get("name") or ""),
                        price=to_float(row.get("trade")),
                        prev_close=to_float(row.get("settlement")),
                        change=to_float(row.get("pricechange")),
                        change_pct=to_float(row.get("changepercent")),
                        amount=to_float(row.get("amount")),
                        volume=to_float(row.get("volume")),
                        turnover=to_float(row.get("turnoverratio")),
                        source=self.name,
                    )
                )
            return out

        gainers = await rank("changepercent", 0)
        losers = await rank("changepercent", 1)
        actives = await rank("amount", 0)
        if not (gainers or losers or actives):
            raise ProviderError("新浪未返回排行榜")
        return {"gainers": gainers, "losers": losers, "actives": actives}
