"""业务编排层：拼装看板列表、详情页数据，统一走缓存。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from . import indicators, storage
from .cache import cache
from .config import settings
from .providers import ProviderError, Quote, registry
from .utils import (
    data_is_stale,
    full_code,
    is_trading_now,
    normalize_code,
    resolve_market,
    session_state,
)

log = logging.getLogger("service")


def quote_ttl() -> float:
    return settings.QUOTE_TTL_OPEN if is_trading_now() else settings.QUOTE_TTL_CLOSED


def history_ttl() -> float:
    return settings.HISTORY_TTL_OPEN if is_trading_now() else settings.HISTORY_TTL_CLOSED


# 上一次成功的数据保留 24 小时。数据源被频控/宕机时，宁可展示带「延迟」标记的
# 旧数据，也不要给用户一片空白（K线、资金、两融都是日频数据，隔夜可用）。
STALE_TTL = 86400.0


async def cached_pack(
    key: str,
    ttl: float,
    loader: Any,
    force: bool,
    *,
    empty: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """带「过期兜底」的缓存读取：取数失败时回退到最近一次成功的结果。"""
    try:
        data = await cache.get_or_set(key, ttl, loader, force=force)
        cache.put(f"stale:{key}", data, STALE_TTL)
        return {**data, "stale": False}
    except Exception as exc:  # noqa: BLE001
        stale = cache.peek(f"stale:{key}")
        if stale is not None:
            log.info("%s 取数失败，回退上一次成功数据：%s", key, exc)
            return {**stale, "stale": True, "error": str(exc)}
        log.info("%s 取数失败且无历史数据：%s", key, exc)
        if empty is None:
            raise
        return {**empty, "stale": False, "error": str(exc)}


# ------------------------------------------------------------------ 行情

async def get_quotes(keys: list[tuple[str, str]], force: bool = False) -> dict[str, Quote]:
    """带缓存的批量行情。缓存粒度是单只股票，便于不同页面复用。"""
    ttl = quote_ttl()
    result: dict[str, Quote] = {}
    missing: list[tuple[str, str]] = []

    for code, market in keys:
        code = normalize_code(code)
        market = market or resolve_market(code)
        key = f"{code}.{market}"
        hit = None if force else cache.peek(f"quote:{key}")
        if hit is not None:
            result[key] = hit
        else:
            missing.append((code, market))

    if missing:
        fetched, _ = await registry().quotes(missing)
        for key, quote in fetched.items():
            cache.put(f"quote:{key}", quote, ttl)
            result[key] = quote
    return result


async def get_quote(code: str, market: str | None = None, force: bool = False) -> Quote:
    code = normalize_code(code)
    market = market or resolve_market(code)
    data = await get_quotes([(code, market)], force=force)
    quote = data.get(f"{code}.{market}")
    if not quote:
        raise ProviderError(f"未获取到 {code} 的行情")
    return quote


# ------------------------------------------------------------------ 自选股看板

async def watchlist_board(force: bool = False) -> dict[str, Any]:
    rows = storage.list_watchlist()
    if not rows:
        return {"items": [], "session": session_info()}

    keys = [(r["code"], r["market"]) for r in rows]
    try:
        quotes = await get_quotes(keys, force=force)
    except ProviderError as exc:
        log.warning("看板行情获取失败：%s", exc)
        quotes = {}

    # 板块兜底：腾讯/新浪行情不带行业字段，eastmoney 被限流时就会出现空板块。
    # 对缺失板块的股票用批量接口一次补齐（1 次请求覆盖多只，逐股 1 小时缓存），
    # 成功即回写数据库，之后即使 eastmoney 持续不可用也能稳定展示。
    missing: list[tuple[str, str]] = []
    for r in rows:
        quote = quotes.get(full_code(r["code"], r["market"]))
        if not (quote.board if quote else "") and not (r.get("board") or ""):
            missing.append((r["code"], r["market"]))
    board_map = await _industry_map(missing) if missing else {}

    items: list[dict[str, Any]] = []
    for row in rows:
        key = full_code(row["code"], row["market"])
        quote = quotes.get(key)
        if quote:
            data = quote.to_dict()
            # 板块以数据源为准，源没给就用入库时记录的
            if not data.get("board"):
                data["board"] = row.get("board") or ""
            if not data.get("name"):
                data["name"] = row.get("name") or ""
            if quote.name and quote.name != row.get("name"):
                storage.update_meta(row["code"], quote.name, quote.board or None)
        else:
            data = Quote(
                code=row["code"],
                market=row["market"],
                name=row.get("name") or row["code"],
                board=row.get("board") or "",
                status="unknown",
                status_text="数据更新延迟，暂无有效行情",
            ).to_dict()
        board = (data.get("board") or "").strip() or board_map.get(key, "")
        if board and board != row.get("board"):
            storage.update_meta(row["code"], None, board)
        data["board"] = board
        data["sort_no"] = row["sort_no"]
        items.append(data)

    return {"items": items, "session": session_info()}


async def _industry_map(keys: list[tuple[str, str]]) -> dict[str, str]:
    """补齐自选股所属行业：批量接口一次覆盖多只（1 小时缓存），

    批量接口被 CDN 限流时回退到逐股 slist 查询（同样 1 小时缓存）。
    """
    found: dict[str, str] = {}
    missing: list[tuple[str, str]] = []
    for code, market in keys:
        key = full_code(code, market)
        hit = cache.peek(f"industry:{key}")
        if hit:
            found[key] = hit
        else:
            missing.append((code, market))
    if missing:
        try:
            fetched = await registry().industry(missing)
        except ProviderError as exc:
            log.info("行业批量查询失败：%s", exc)
            fetched = {}
        for key, name in fetched.items():
            cache.put(f"industry:{key}", name, 3600.0)
            found[key] = name
        # 批量接口没覆盖到的（失败/空结果），并发逐股兜底，避免串行拖慢看板接口
        still = [(c, m) for c, m in missing if full_code(c, m) not in found]
        if still:
            async def _fetch_board(item: tuple[str, str]) -> tuple[str, str, str | None]:
                code, market = item
                names = await _boards(code, market, False)
                return code, market, (names[0] if names else None)

            results = await asyncio.gather(
                *(_fetch_board(item) for item in still), return_exceptions=True
            )
            for r in results:
                if isinstance(r, BaseException):
                    log.info("逐股板块兜底失败：%s", r)
                    continue
                code, market, name = r
                if name:
                    key = full_code(code, market)
                    cache.put(f"industry:{key}", name, 3600.0)
                    found[key] = name
    return found


def session_info() -> dict[str, Any]:
    state = session_state()
    trading = is_trading_now()
    return {
        "state": state,
        "trading": trading,
        "auto_refresh": trading,
        "interval_ms": 3000 if trading else 0,
        "label": {
            "open": "盘中交易",
            "lunch_break": "午间休市",
            "pre_open": "尚未开盘",
            "closed": "已收盘",
            "weekend": "休市中",
        }.get(state, state),
    }


# ------------------------------------------------------------------ 详情页

async def _kline(code: str, market: str, force: bool) -> dict[str, Any]:
    async def loader() -> Any:
        bars, src = await registry().kline(code, market, settings.KLINE_LIMIT)
        return {"bars": bars, "source": src}

    return await cached_pack(f"kline:{code}.{market}", history_ttl(), loader, force)


async def _flow(code: str, market: str, force: bool) -> dict[str, Any]:
    async def loader() -> Any:
        rows, src = await registry().fund_flow(code, market, settings.FLOW_DAYS)
        return {"rows": rows, "source": src}

    return await cached_pack(
        f"flow:{code}.{market}", history_ttl(), loader, force,
        empty={"rows": [], "source": ""},
    )


async def _margin(code: str, market: str, force: bool) -> dict[str, Any]:
    async def loader() -> Any:
        rows, src = await registry().margin(code, market, settings.MARGIN_DAYS)
        return {"rows": rows, "source": src}

    return await cached_pack(
        f"margin:{code}.{market}", history_ttl(), loader, force,
        empty={"rows": [], "source": ""},
    )


async def _boards(code: str, market: str, force: bool) -> list[str]:
    async def loader() -> Any:
        return {"names": await registry().boards(code, market)}

    pack = await cached_pack(
        f"boards:{code}.{market}", 3600.0, loader, force, empty={"names": []}
    )
    return [n for n in (pack.get("names") or []) if n]


async def boards(code: str, market: str | None = None) -> list[str]:
    """公开的板块查询（1 小时缓存），用于添加自选等轻量场景。"""
    code = normalize_code(code)
    market = market or resolve_market(code)
    return await _boards(code, market, False)


async def stock_detail(code: str, market: str | None = None, force: bool = False) -> dict[str, Any]:
    code = normalize_code(code)
    market = market or resolve_market(code)

    # 并发取数；任一失败立即取消其余任务，避免孤儿协程继续打上游接口
    # （FIRST_EXCEPTION：无异常时等全部完成，有异常时立刻返回并取消未完成的）
    tasks = {
        "quote": asyncio.create_task(get_quote(code, market, force=force)),
        "kline": asyncio.create_task(_kline(code, market, force)),
        "flow": asyncio.create_task(_flow(code, market, force)),
        "margin": asyncio.create_task(_margin(code, market, force)),
        "boards": asyncio.create_task(_boards(code, market, force)),
    }
    done, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_EXCEPTION)
    for t in done:
        exc = t.exception()
        if exc is not None:
            for p in pending:
                p.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise exc

    quote = tasks["quote"].result()
    kline_pack = tasks["kline"].result()
    flow_pack = tasks["flow"].result()
    margin_pack = tasks["margin"].result()
    boards = tasks["boards"].result()

    bars = kline_pack["bars"]
    ma_infos, ma_summary = indicators.build_ma(bars, quote.price)
    ma_values = {i.window: i.value for i in ma_infos}
    sr = indicators.support_resistance(bars, quote.price, ma_values)
    flow_summary = indicators.summarize_flow(flow_pack["rows"])
    margin_summary = indicators.summarize_margin(margin_pack["rows"])
    status = indicators.build_status(quote, bars, flow_summary, margin_summary, ma_summary, sr)

    quote_dict = quote.to_dict()
    if not quote_dict.get("board") and boards:
        quote_dict["board"] = boards[0]
        # 回写数据库，看板页从此不再依赖行情源是否携带行业字段
        storage.update_meta(code, None, boards[0])

    last_bar_date = bars[-1].date if bars else ""
    if quote_dict["status"] == "normal" and data_is_stale(last_bar_date):
        quote_dict["status"] = "delayed"
        quote_dict["status_text"] = f"数据更新延迟（最新交易日 {last_bar_date}）"

    return {
        "quote": quote_dict,
        "boards": boards,
        "watched": storage.is_watched(code),
        "kline": [asdict(b) for b in bars[-120:]],
        "ma": [asdict(m) for m in ma_infos],
        "ma_summary": {
            **ma_summary,
            "series": {
                name: seq[-120:] for name, seq in ma_summary["series"].items()
            },
        },
        "support_resistance": sr,
        "fund_flow": {
            "rows": [asdict(r) for r in flow_pack["rows"]],
            "summary": flow_summary,
            "source": flow_pack.get("source", ""),
            "stale": flow_pack.get("stale", False),
            "error": flow_pack.get("error", ""),
        },
        "margin": {
            "rows": [asdict(r) for r in margin_pack["rows"]],
            "summary": margin_summary,
            "source": margin_pack.get("source", ""),
            "stale": margin_pack.get("stale", False),
            "error": margin_pack.get("error", ""),
        },
        "status": status,
        "sources": {
            "quote": quote.source,
            "kline": kline_pack.get("source", ""),
            "fund_flow": flow_pack.get("source", ""),
            "margin": margin_pack.get("source", ""),
            "stale": [
                name for name, pack in
                (("K线", kline_pack), ("资金流向", flow_pack), ("两融", margin_pack))
                if pack.get("stale")
            ],
        },
        "session": session_info(),
    }


# ------------------------------------------------------------------ 搜索 / 热门

async def search(keyword: str, limit: int = 15) -> list[dict[str, Any]]:
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    async def loader() -> Any:
        return await registry().search(keyword, limit)

    try:
        items = await cache.get_or_set(
            f"search:{keyword.lower()}:{limit}", settings.SEARCH_TTL, loader
        )
    except ProviderError as exc:
        log.info("搜索失败 %s: %s", keyword, exc)
        return []

    keys = [(i.code, i.market) for i in items]
    try:
        quotes = await get_quotes(keys)
    except ProviderError:
        quotes = {}

    out: list[dict[str, Any]] = []
    watched = storage.watched_codes()
    for item in items:
        key = f"{item.code}.{item.market}"
        quote = quotes.get(key)
        row: dict[str, Any] = {
            "code": item.code,
            "market": item.market,
            "name": item.name,
            "board": (quote.board if quote else "") or item.board,
            "price": quote.price if quote else None,
            "prev_close": quote.prev_close if quote else None,
            "change_pct": quote.change_pct if quote else None,
            "status": quote.status if quote else "unknown",
            "status_text": quote.status_text if quote else "",
            "watched": item.code in watched,
        }
        out.append(row)
    return out


async def hot(limit: int = 8) -> dict[str, Any]:
    async def loader() -> Any:
        return {"data": await registry().hot(limit)}

    pack = await cached_pack(
        f"hot:{limit}", settings.HOT_TTL, loader, False,
        empty={"data": {"gainers": [], "losers": [], "actives": []}},
    )
    data = pack.get("data") or {}
    watched = storage.watched_codes()

    def pack_rows(items: list[Quote]) -> list[dict[str, Any]]:
        return [{**q.to_dict(), "watched": q.code in watched} for q in items]

    return {
        **{k: pack_rows(v) for k, v in data.items()},
        "stale": pack.get("stale", False),
        "error": pack.get("error", ""),
    }
