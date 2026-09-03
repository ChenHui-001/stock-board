"""热点搜索：真正打到服务端的检索，双轨。

- **站内轨（默认，免费）**：东方财富全文检索（`providers.eastmoney.search_articles`），
  覆盖东财站内财经文章库（`hitsTotal` 常达上万），关键词任意。
- **全网轨（可选）**：配了 `SEARCH_API_KEY` 才启用，走通用搜索 API
  （Serper / Tavily 二选一，`SEARCH_API_PROVIDER` 指定）。

两轨返回同一 shape（与 `hotspot.py` 的条目字段一致，前端可直接复用 renderItem），
并带 `engine` 字段供前端显示来源徽标。结果按 (engine, q, days, limit) 缓存。

与旧行为的区别：搜索框原先只在前端对已加载的 ≤40 条快讯做子串匹配
（"只能搜当前页面"），现在关键词直达上游检索库。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from .cache import cache
from .config import settings
from .hotspot import is_hot_media
from .providers.base import ProviderError, client
from .providers.eastmoney import clean_em, search_articles
from .utils import TZ, now

log = logging.getLogger("hotspot.search")

TTL = settings.HOTSPOT_SEARCH_TTL
_WEB_TIMEOUT = 12.0        # 全网搜索 API 单次调用超时（秒）
_MAX_SUMMARY = 300         # 摘要截断，避免超长正文压垮列表渲染


def engine_name() -> str:
    """当前生效的检索引擎：配了 key 走全网轨，否则站内轨。"""
    if settings.SEARCH_API_KEY:
        provider = settings.SEARCH_API_PROVIDER
        if provider in ("serper", "tavily"):
            return f"web:{provider}"
        log.warning("SEARCH_API_PROVIDER=%s 不支持，回退站内全文检索", provider)
    return "eastmoney"


def engine_label(engine: str) -> str:
    return {
        "eastmoney": "东方财富全文检索",
        "web:serper": "全网搜索（Serper）",
        "web:tavily": "全网搜索（Tavily）",
    }.get(engine, engine)


# ------------------------------------------------------------------ 站内轨

def _parse_em_date(value: str) -> int | None:
    """'YYYY-MM-DD HH:MM:SS' → unix 秒；只有日期时补 00:00:00。"""
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=TZ).timestamp())
        except ValueError:
            continue
    return None


async def _search_eastmoney(q: str, days: int, limit: int) -> list[dict[str, Any]]:
    """东财站内全文检索。按 days 过滤后取前 limit 条。"""
    rows = await search_articles(q, page_size=max(limit * 2, 30))
    cutoff = now().timestamp() - days * 86400
    out: list[dict[str, Any]] = []
    for row in rows:
        title = clean_em(row.get("title"))
        ts = _parse_em_date(str(row.get("date") or ""))
        if not title or ts is None or ts < cutoff:
            continue
        source = clean_em(row.get("mediaName")) or "东方财富"
        out.append({
            "id": str(row.get("code") or ""),
            "title": title,
            "summary": clean_em(row.get("content"))[:_MAX_SUMMARY],
            "ts": ts,
            "source": source,
            "origin": "东财检索",
            "url": str(row.get("url") or "").strip(),
        })
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ 全网轨

async def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """全网搜索 API 的一次 JSON POST。失败一律翻成 ProviderError。"""
    try:
        resp = await client().post(url, json=payload, headers=headers, timeout=_WEB_TIMEOUT)
    except httpx.HTTPError as exc:
        raise ProviderError(f"全网搜索请求失败：{type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise ProviderError(f"全网搜索 API Key 无效或额度不足（HTTP {resp.status_code}）")
    if resp.status_code >= 400:
        raise ProviderError(f"全网搜索接口返回 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderError("全网搜索接口返回非 JSON") from exc
    return data if isinstance(data, dict) else {}


def _rel_date_to_ts(value: str) -> int | None:
    """全网搜索返回的日期形如 '2 hours ago' / '2026-08-24'，尽力解析，失败给 None。"""
    v = (value or "").strip().lower()
    if not v:
        return None
    ts = _parse_em_date(v[:19].replace("t", " "))
    if ts is not None:
        return ts
    import re
    m = re.match(r"(\d+)\s*(minute|hour|day|week|month)s?\s*ago", v)
    if not m:
        return None
    n = int(m.group(1))
    unit = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000}[m.group(2)]
    return int(now().timestamp() - n * unit)


async def _search_serper(q: str, days: int, limit: int) -> list[dict[str, Any]]:
    """Serper（google.serper.dev）新闻检索：一次 JSON POST。"""
    window = "qdr:d" if days <= 1 else ("qdr:w" if days <= 7 else "qdr:m")
    data = await _post_json(
        "https://google.serper.dev/news",
        {"q": q, "gl": "cn", "hl": "zh-cn", "num": min(max(limit, 10), 40), "tbs": window},
        {"X-API-KEY": settings.SEARCH_API_KEY, "Content-Type": "application/json"},
    )
    out: list[dict[str, Any]] = []
    for row in data.get("news") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "id": "",
            "title": title,
            "summary": str(row.get("snippet") or "").strip()[:_MAX_SUMMARY],
            "ts": _rel_date_to_ts(str(row.get("date") or "")),
            "source": str(row.get("source") or "").strip() or "全网",
            "origin": "全网搜索",
            "url": str(row.get("link") or "").strip(),
        })
        if len(out) >= limit:
            break
    return out


async def _search_tavily(q: str, days: int, limit: int) -> list[dict[str, Any]]:
    """Tavily 检索：一次 JSON POST。"""
    data = await _post_json(
        "https://api.tavily.com/search",
        {
            "api_key": settings.SEARCH_API_KEY,
            "query": q,
            "topic": "news",
            "days": max(int(days), 1),
            "max_results": min(max(limit, 5), 20),
            "search_depth": "basic",
        },
        {"Content-Type": "application/json"},
    )
    out: list[dict[str, Any]] = []
    for row in data.get("results") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        url = str(row.get("url") or "").strip()
        out.append({
            "id": "",
            "title": title,
            "summary": str(row.get("content") or "").strip()[:_MAX_SUMMARY],
            "ts": _rel_date_to_ts(str(row.get("published_date") or "")),
            "source": (httpx.URL(url).host or "全网") if url else "全网",
            "origin": "全网搜索",
            "url": url,
        })
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ 组装入口

async def _load(q: str, days: int, limit: int, engine: str) -> dict[str, Any]:
    if engine == "web:serper":
        items = await _search_serper(q, days, limit)
    elif engine == "web:tavily":
        items = await _search_tavily(q, days, limit)
    else:
        items = await _search_eastmoney(q, days, limit)

    # 有时间戳的按时间倒序在前，无时间戳的（部分全网结果）保持上游相关度顺序排在后面
    dated = sorted([x for x in items if x.get("ts")], key=lambda x: x["ts"], reverse=True)
    undated = [x for x in items if not x.get("ts")]
    merged = dated + undated
    for it in merged:
        ts = it.get("ts")
        it["time"] = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        it["media_badge"] = is_hot_media(it.get("source", ""))
        it["kind"] = "search"
    return {
        "items": merged,
        "meta": {
            "keyword": q,
            "days": days,
            "total": len(merged),
            "engine": engine,
            "engine_label": engine_label(engine),
            "fetched_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


async def search(q: str, days: int = 7, limit: int = 30, force: bool = False) -> dict[str, Any]:
    """按关键词检索热点资讯。q 为空直接返回空结果（不打上游）。"""
    q = (q or "").strip()
    days = min(max(int(days), 1), 30)
    limit = min(max(int(limit), 1), 50)
    if not q:
        return {
            "items": [],
            "meta": {
                "keyword": "", "days": days, "total": 0,
                "engine": engine_name(), "engine_label": engine_label(engine_name()),
                "fetched_at": now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }
    engine = engine_name()
    key = f"hsearch:{engine}:{q}:{days}:{limit}"
    try:
        return await cache.get_or_set(key, TTL, lambda: _load(q, days, limit, engine), force=force)
    except ProviderError as exc:
        # 全网轨故障（key 失效/额度耗尽）时兜到站内轨，别让搜索整个不可用
        if engine != "eastmoney":
            log.warning("全网搜索失败，回退站内全文检索：%s", exc)
            try:
                fallback = await cache.get_or_set(
                    f"hsearch:eastmoney:{q}:{days}:{limit}", TTL,
                    lambda: _load(q, days, limit, "eastmoney"), force=force,
                )
                fallback["meta"]["fallback_from"] = engine
                fallback["meta"]["fallback_reason"] = str(exc)
                return fallback
            except ProviderError as exc2:
                log.debug("%s 降级: %s", "search", exc2)
                exc = exc2
        result: dict[str, Any] = {
            "items": [],
            "meta": {
                "keyword": q, "days": days, "total": 0,
                "engine": engine, "engine_label": engine_label(engine),
                "error": f"搜索失败：{exc}",
                "fetched_at": now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }
        cache.put(key, result, min(TTL, 30.0))
        return result
