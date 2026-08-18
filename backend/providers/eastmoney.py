"""东方财富数据源（主）：行情 / 搜索 / K线 / 资金流向 / 两融 / 热门榜 / 板块。"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from ..utils import TZ, normalize_code, resolve_market, secid, to_float
from .base import (
    Bar,
    FlowDay,
    MarginDay,
    NewsItem,
    Provider,
    ProviderError,
    Quote,
    SearchItem,
    fetch,
)

PUSH = "https://push2.eastmoney.com/api/qt"
PUSH_HIS = "https://push2his.eastmoney.com/api/qt"
SEARCH = "https://searchapi.eastmoney.com/api/suggest/get"
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REFERER = {"Referer": "https://quote.eastmoney.com/"}

# ulist 字段（f10=量比，f8=换手率）
# f86 在 stock/get（单股）接口是标准 unix 时间戳，但在 ulist（批量）接口部分场景
# 返回的是延迟秒数等非时间戳值；f124 是批量接口的备选更新时间字段，二者都无效时
# trade_date 置空，由上层用 K 线日期回填（详情页已实现）。
QUOTE_FIELDS = "f1,f2,f3,f4,f5,f6,f8,f10,f12,f13,f14,f15,f16,f17,f18,f86,f100,f124,f292"

# A 股全市场（沪深主板+创业板+科创板+北交所）
FS_ALL = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

_STATUS_TEXT = {
    "suspended": "股票停牌",
    "delayed": "数据更新延迟",
    "delisted": "股票已退市",
}


def _ts_to_date(value: Any) -> str:
    """f86 为行情更新的 unix 时间戳；非法值（含 "-"、0、小整数）一律忽略。"""
    ts = to_float(value)
    if not ts or ts < 946684800:  # 2000-01-01 之前视为无效
        return ""
    try:
        return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _market_from_f13(f13: Any, code: str) -> str:
    if f13 in (1, "1"):
        return "SH"
    guess = resolve_market(code)
    return "BJ" if guess == "BJ" else "SZ"


class EastmoneyProvider(Provider):
    def __init__(self) -> None:
        super().__init__(
            name="eastmoney",
            caps={"quotes", "search", "kline", "fund_flow", "margin", "hot", "boards", "industry", "news"},
        )

    # ------------------------------------------------------------ 实时行情
    async def quotes(self, keys: list[tuple[str, str]]) -> dict[str, Quote]:
        if not keys:
            return {}
        secids = ",".join(secid(c, m) for c, m in keys)
        resp = await fetch(
            f"{PUSH}/ulist.np/get",
            headers=REFERER,
            params={
                "fltt": 2,
                "invt": 2,
                "fields": QUOTE_FIELDS,
                "secids": secids,
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            },
        )
        payload = resp.json()
        diff = ((payload or {}).get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            raise ProviderError("东方财富未返回行情数据")

        out: dict[str, Quote] = {}
        for row in diff:
            code = normalize_code(str(row.get("f12") or ""))
            if not code:
                continue
            market = _market_from_f13(row.get("f13"), code)
            quote = Quote(
                code=code,
                market=market,
                name=str(row.get("f14") or ""),
                board=str(row.get("f100") or "").strip("-") or "",
                price=to_float(row.get("f2")),
                prev_close=to_float(row.get("f18")),
                change=to_float(row.get("f4")),
                change_pct=to_float(row.get("f3")),
                open=to_float(row.get("f17")),
                high=to_float(row.get("f15")),
                low=to_float(row.get("f16")),
                volume=(to_float(row.get("f5"), 0.0) or 0.0) * 100,  # 手 -> 股
                amount=to_float(row.get("f6")),
                turnover=to_float(row.get("f8")),
                volume_ratio=to_float(row.get("f10")),
                trade_date=_ts_to_date(row.get("f86")) or _ts_to_date(row.get("f124")),
                source=self.name,
            )
            out[f"{code}.{market}"] = quote
        return out

    # ------------------------------------------------------------ 搜索
    async def search(self, keyword: str, limit: int = 15) -> list[SearchItem]:
        resp = await fetch(
            SEARCH,
            params={
                "input": keyword,
                "type": 14,
                "token": "D43BF722C8E33BDC906FB84D85E326E8",
                # 联想接口会混入基金/债券/港股等非 A 股条目，预留 2 倍余量过滤；
                # 上限 60 防止超大 limit 无谓拉取（此前固定 3 倍、默认 15 条会拉到 45 条）
                "count": min(max(limit * 2, 20), 60),
            },
        )
        data = resp.json() or {}
        rows = ((data.get("QuotationCodeTable") or {}).get("Data")) or []
        items: list[SearchItem] = []
        for row in rows:
            if row.get("Classify") not in ("AStock", "AStockSH", "AStockSZ"):
                continue
            code = normalize_code(str(row.get("Code") or ""))
            if not code or not code.isdigit() or len(code) != 6:
                continue
            market = "SH" if str(row.get("MktNum")) == "1" else resolve_market(code)
            if market == "SZ" and resolve_market(code) == "BJ":
                market = "BJ"
            items.append(
                SearchItem(
                    code=code,
                    market=market,
                    name=str(row.get("Name") or ""),
                    type=str(row.get("SecurityTypeName") or "A股"),
                )
            )
            if len(items) >= limit:
                break
        return items

    # ------------------------------------------------------------ K 线
    async def kline(self, code: str, market: str, limit: int, klt: int = 101) -> list[Bar]:
        """日线（默认）/分钟线。klt=5 五分钟线等；分钟线日期带时间戳（YYYY-MM-DD HH:MM）。"""
        resp = await fetch(
            f"{PUSH_HIS}/stock/kline/get",
            headers=REFERER,
            params={
                "secid": secid(code, market),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": klt,
                "fqt": 1,     # 前复权
                "end": "20500101",
                "lmt": limit,
            },
        )
        klines = ((resp.json() or {}).get("data") or {}).get("klines") or []
        if not klines:
            raise ProviderError("东方财富未返回K线")
        bars: list[Bar] = []
        for line in klines:
            p = line.split(",")
            if len(p) < 7:
                continue
            bars.append(
                Bar(
                    date=p[0],
                    open=to_float(p[1], 0.0),
                    close=to_float(p[2], 0.0),
                    high=to_float(p[3], 0.0),
                    low=to_float(p[4], 0.0),
                    volume=(to_float(p[5], 0.0) or 0) * 100,
                    amount=to_float(p[6], 0.0) or 0.0,
                    change_pct=to_float(p[8]) if len(p) > 8 else None,
                    turnover=to_float(p[10]) if len(p) > 10 else None,
                )
            )
        return bars

    # ------------------------------------------------------------ 资金流向
    async def fund_flow(self, code: str, market: str, days: int) -> list[FlowDay]:
        resp = await fetch(
            f"{PUSH_HIS}/stock/fflow/daykline/get",
            headers=REFERER,
            params={
                "secid": secid(code, market),
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "klt": 101,
                "lmt": days,
            },
        )
        klines = ((resp.json() or {}).get("data") or {}).get("klines") or []
        rows: list[FlowDay] = []
        for line in klines:
            p = line.split(",")
            if len(p) < 6:
                continue
            rows.append(
                FlowDay(
                    date=p[0],
                    main=to_float(p[1], 0.0) or 0.0,
                    sm=to_float(p[2], 0.0) or 0.0,
                    md=to_float(p[3], 0.0) or 0.0,
                    lg=to_float(p[4], 0.0) or 0.0,
                    xl=to_float(p[5], 0.0) or 0.0,
                    main_pct=to_float(p[6]) if len(p) > 6 else None,
                    close=to_float(p[11]) if len(p) > 11 else None,
                    change_pct=to_float(p[12]) if len(p) > 12 else None,
                )
            )
        return rows[-days:]

    # ------------------------------------------------------------ 两融
    async def margin(self, code: str, market: str, days: int) -> list[MarginDay]:
        resp = await fetch(
            DC,
            headers=REFERER,
            params={
                "reportName": "RPTA_WEB_RZRQ_GGMX",
                "columns": "ALL",
                "filter": f'(SCODE="{normalize_code(code)}")',
                "pageNumber": 1,
                "pageSize": days,
                "sortColumns": "DATE",
                "sortTypes": -1,
                "source": "WEB",
                "client": "WEB",
            },
        )
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderError("两融接口返回非 JSON") from exc
        rows = ((payload or {}).get("result") or {}).get("data") or []
        out: list[MarginDay] = []
        for row in rows:
            out.append(
                MarginDay(
                    date=str(row.get("DATE") or "")[:10],
                    rzye=to_float(row.get("RZYE")),
                    rzmre=to_float(row.get("RZMRE")),
                    rzche=to_float(row.get("RZCHE")),
                    rzjme=to_float(row.get("RZJME")),
                    rqye=to_float(row.get("RQYE")),
                    rqmcl=to_float(row.get("RQMCL")),
                    rqyl=to_float(row.get("RQYL")),
                    rzrqye=to_float(row.get("RZRQYE")),
                    rzyezb=to_float(row.get("RZYEZB")),
                )
            )
        out.reverse()  # 时间正序
        return out

    # ------------------------------------------------------------ 板块
    async def boards(self, code: str, market: str) -> list[str]:
        resp = await fetch(
            f"{PUSH}/slist/get",
            headers=REFERER,
            params={
                "fltt": 2,
                "invt": 2,
                "fields": "f12,f13,f14,f3",
                "secid": secid(code, market),
                "pn": 1,
                "pz": 30,
                "po": 1,
                "spt": 3,
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            },
        )
        diff = ((resp.json() or {}).get("data") or {}).get("diff") or {}
        if isinstance(diff, dict):
            diff = list(diff.values())
        names = [str(x.get("f14") or "").strip() for x in diff]
        return [n for n in names if n]

    # ------------------------------------------------------------ 批量行业
    async def industry(self, keys: list[tuple[str, str]]) -> dict[str, str]:
        """一次 ulist 请求返回多只股票的所属行业（f100，细分行业）。

        替代逐股 slist 查询：自选股较多时从 N 次请求降为 1 次。
        """
        if not keys:
            return {}
        secids = ",".join(secid(c, m) for c, m in keys)
        resp = await fetch(
            f"{PUSH}/ulist.np/get",
            headers=REFERER,
            params={
                "fltt": 2,
                "invt": 2,
                "fields": "f12,f13,f14,f100",
                "secids": secids,
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            },
        )
        diff = ((resp.json() or {}).get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        out: dict[str, str] = {}
        for row in diff:
            code = normalize_code(str(row.get("f12") or ""))
            if not code:
                continue
            market = _market_from_f13(row.get("f13"), code)
            name = str(row.get("f100") or "").strip(" -")
            if name:
                out[f"{code}.{market}"] = name
        return out

    # ------------------------------------------------------------ 个股资讯
    async def news(
        self, code: str, market: str, name: str, days: int = 30, limit: int = 15
    ) -> list[NewsItem]:
        """东财搜索接口（与 AkShare stock_news_em 同源）：按股票名称检索财经新闻。

        用代码检索会命中正文含该数字的无关文章（如"回购 600000 股"），
        因此优先用股票名称；名称缺失时才退回代码。返回按时间倒序。
        """
        keyword = (name or "").strip() or normalize_code(code)
        param = {
            "uid": "",
            "keyword": keyword,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": max(limit * 3, 30),  # 多拉一些，过滤非本股/超期文章
                    "preTag": "<em>",
                    "postTag": "</em>",
                }
            },
        }
        resp = await fetch(
            "https://search-api-web.eastmoney.com/search/jsonp",
            headers={"Referer": "https://so.eastmoney.com/"},
            params={
                "cb": "jQuery35109092213424858634_1653090224873",
                "param": json.dumps(param, ensure_ascii=False),
                "_": str(int(time.time() * 1000)),
            },
        )
        text = resp.text or ""
        start, end = text.find("("), text.rfind(")")
        if start < 0 or end <= start:
            raise ProviderError("东方财富资讯接口返回非 JSONP")
        try:
            payload = json.loads(text[start + 1 : end])
        except json.JSONDecodeError as exc:
            raise ProviderError("东方财富资讯接口 JSON 解析失败") from exc
        rows = ((payload or {}).get("result") or {}).get("cmsArticleWebOld") or []

        def _clean(s: Any) -> str:
            return str(s or "").replace("<em>", "").replace("</em>", "").strip()

        out: list[NewsItem] = []
        today = datetime.now(TZ).date()
        for row in rows:
            date = str(row.get("date") or "").strip()
            try:
                day = datetime.strptime(date[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if (today - day).days > days:
                continue
            out.append(
                NewsItem(
                    id=str(row.get("code") or ""),
                    date=date,
                    source=_clean(row.get("mediaName")),
                    title=_clean(row.get("title")),
                    summary=_clean(row.get("content")),
                    url=str(row.get("url") or "").strip(),
                )
            )
            if len(out) >= limit:
                break
        if not out:
            raise ProviderError("东方财富未返回该股票的近期资讯")
        return out

    # ------------------------------------------------------------ 热门榜
    async def hot(self, limit: int = 10) -> dict[str, list[Quote]]:
        async def rank(fid: str, order: int) -> list[Quote]:
            resp = await fetch(
                f"{PUSH}/clist/get",
                headers=REFERER,
                params={
                    "pn": 1,
                    "pz": limit,
                    "po": order,
                    "np": 1,
                    "fltt": 2,
                    "invt": 2,
                    "fid": fid,
                    "fs": FS_ALL,
                    "fields": "f2,f3,f6,f12,f13,f14,f18,f100",
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                },
            )
            diff = ((resp.json() or {}).get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            out: list[Quote] = []
            for row in diff:
                code = normalize_code(str(row.get("f12") or ""))
                if not code:
                    continue
                market = _market_from_f13(row.get("f13"), code)
                price = to_float(row.get("f2"))
                prev = to_float(row.get("f18"))
                out.append(
                    Quote(
                        code=code,
                        market=market,
                        name=str(row.get("f14") or ""),
                        board=str(row.get("f100") or "").strip("-") or "",
                        price=price if price else prev,   # 盘前无成交价时展示昨收
                        prev_close=prev,
                        change_pct=to_float(row.get("f3"), 0.0),
                        amount=to_float(row.get("f6")),
                        source=self.name,
                    )
                )
            return out

        gainers = await rank("f3", 1)   # 涨幅榜
        losers = await rank("f3", 0)    # 跌幅榜
        actives = await rank("f6", 1)   # 成交额榜
        return {"gainers": gainers, "losers": losers, "actives": actives}
