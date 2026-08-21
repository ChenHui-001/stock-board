"""市场热点追踪：聚合多源 7x24 快讯，展示近 N 分钟内热点资讯。

数据源均为公开网页/接口加载的真实数据（非模拟）：
- 同花顺 7x24 快讯  news.10jqka.com.cn/tapp/news/push/stock/
- 东方财富 快讯    np-listapi.eastmoney.com/comm/web/getNewsByColumns
- 新浪财经 7x24    zhibo.sina.com.cn/api/zhibo/feed
- 华尔街见闻 7x24  api-one.wallstcn.com/apiv1/content/lives

各源并行抓取 → 统一成条目 → 按时间窗过滤 → 按标题指纹去重 → 按时间倒序。
媒体署名（彭博社/财联社/财新/澎湃等）保留在 source 字段，命中《重点媒体》
名单的条目加 media_badge 标记，便于前端优先突出展示。

结果整体进内存缓存（默认 90s），避免反复打外部快讯接口。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from .cache import cache
from .config import settings
from .providers.base import ProviderError, fetch
from .utils import TZ, now

log = logging.getLogger("hotspot")

HOTSPOT_MINUTES = settings.HOTSPOT_MINUTES  # 默认时间窗（分钟，环境变量可配）
HOTSPOT_LIMIT = 40        # 单次最多返回条数
TTL = settings.HOTSPOT_TTL  # 聚合结果缓存（秒，环境变量可配）

# 用户点名的重点媒体：命中即标 media_badge（彭博社/财联社/财新/澎湃/同花顺/东方财富…）
_HOT_MEDIA = (
    "彭博", "财联社", "财新", "澎湃", "同花顺", "东方财富",
    "券商中国", "央视", "新华社", "证券时报", "上海证券报", "中国证券报",
    "第一财经", "界面", "每日经济新闻", "21世纪经济报道", "华尔街见闻",
)

_FETCH_TIMEOUT = 15.0  # 单源抓取超时（秒）


def _is_hot_media(source: str) -> bool:
    """媒体署名是否命中重点媒体名单（彭博/财联社/财新/澎湃/同花顺/东方财富…）。"""
    return any(k in (source or "") for k in _HOT_MEDIA)

# ------------------------------------------------------------------ 时间解析

def _to_ts(value: Any) -> int | None:
    """unix 秒（同花顺 ctime）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: str) -> datetime | None:
    """'YYYY-MM-DD HH:MM:SS' 等格式 → 带时区的 datetime。"""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def _in_window(ts: int | None, minutes: int) -> bool:
    """unix 秒是否落在最近 minutes 分钟内。"""
    if ts is None:
        return False
    return ts >= now().timestamp() - minutes * 60


# ------------------------------------------------------------------ 各源解析
# 解析函数为纯函数（输入原始文本，输出条目），便于离线单测。

def _parse_ths(text: str) -> list[dict[str, Any]]:
    """同花顺 7x24 快讯：{data:{list:[{id,title,digest,url,ctime(秒),source}]}}"""
    try:
        payload = json.loads(text)
        rows = (payload.get("data") or {}).get("list") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        ts = _to_ts(row.get("ctime"))
        if not title or ts is None:
            continue
        out.append({
            "id": str(row.get("id") or ""),
            "title": title,
            "summary": str(row.get("digest") or "").strip(),
            "ts": ts,
            "source": str(row.get("source") or "").strip() or "同花顺",
            "origin": "同花顺",
            "url": str(row.get("url") or "").strip(),
        })
    return out


def _parse_em(text: str) -> list[dict[str, Any]]:
    """东方财富快讯：{data:{list:[{code,title,summary,showTime,mediaName,url}]}}"""
    try:
        payload = json.loads(text)
        rows = (payload.get("data") or {}).get("list") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        ts = _parse_dt(str(row.get("showTime") or ""))
        if ts is None:
            continue
        out.append({
            "id": str(row.get("code") or ""),
            "title": title,
            "summary": str(row.get("summary") or "").strip(),
            "ts": int(ts.timestamp()),
            "source": str(row.get("mediaName") or "").strip() or "东方财富",
            "origin": "东方财富",
            "url": str(row.get("url") or "").strip(),
        })
    return out


_RICH_RE = re.compile(r"^【([^】]+)】\s*(.*)$", re.S)


def _split_rich(rich: str) -> tuple[str, str]:
    """新浪直播正文形如【标题】摘要；拆出标题与摘要。"""
    m = _RICH_RE.match(rich)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return rich[:60], rich


def _split_wscn(text: str) -> tuple[str, str]:
    """华尔街见闻正文：内容通常无【】包裹，取首句（≤60 字）为标题，其余为摘要。"""
    text = (text or "").strip()
    if not text:
        return "", ""
    for sep in ("。", "！", "？", "；", "\n"):
        idx = text.find(sep)
        if 0 < idx <= 60:
            return text[: idx + 1], text[idx + 1 :].strip()
    return text[:42], text


def _parse_wscn(text: str) -> list[dict[str, Any]]:
    """华尔街见闻 7x24：{code,data:{items:[{id,title(常空),content_text,display_time(秒),uri}]}}"""
    try:
        payload = json.loads(text)
        items = (payload.get("data") or {}).get("items") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    out: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        content = str(row.get("content_text") or "").strip()
        ts = _to_ts(row.get("display_time"))
        if not content or ts is None:
            continue
        title, summary = _split_wscn(content)
        if not title:
            continue
        out.append({
            "id": str(row.get("id") or ""),
            "title": title,
            "summary": summary,
            "ts": ts,
            "source": "华尔街见闻",
            "origin": "华尔街见闻",
            "url": str(row.get("uri") or "").strip(),
        })
    return out


def _parse_sina(text: str) -> list[dict[str, Any]]:
    """新浪财经 7x24：{result:{data:{feed:{list:[{id,create_time,rich_text,docurl}]}}}}"""
    try:
        payload = json.loads(text)
        feed = ((payload.get("result") or {}).get("data") or {}).get("feed") or {}
        rows = feed.get("list") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rich = str(row.get("rich_text") or "").strip()
        if not rich:
            continue
        ts = _parse_dt(str(row.get("create_time") or ""))
        if ts is None:
            continue
        title, summary = _split_rich(rich)
        if not title:
            continue
        out.append({
            "id": str(row.get("id") or ""),
            "title": title,
            "summary": summary,
            "ts": int(ts.timestamp()),
            "source": "新浪财经",
            "origin": "新浪财经",
            "url": str(row.get("docurl") or "").strip(),
        })
    return out


# ------------------------------------------------------------------ 抓取与合并

_FEEDS: list[tuple[str, str, dict[str, str], Any]] = [
    ("同花顺", "https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize=50",
     {"Referer": "https://news.10jqka.com.cn/"}, _parse_ths),
    ("东方财富", "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col&column=345&order=1&needInteractData=0&page_index=1&page_size=50&req_trace=hotspot",
     {"Referer": "https://finance.eastmoney.com/"}, _parse_em),
    ("新浪财经", "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=50&zhibo_id=152&tag_id=0&dire=f&dpc=1",
     {"Referer": "https://finance.sina.com.cn/"}, _parse_sina),
    ("华尔街见闻", "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=50",
     {"Referer": "https://wallstreetcn.com/"}, _parse_wscn),
]


async def _fetch_one(
    name: str, url: str, headers: dict[str, str], parse: Any, minutes: int
) -> tuple[list[dict[str, Any]], bool, str]:
    """抓取并解析单个源，只保留时间窗内的条目。失败返回 ([]，False, 原因)。

    东财等源偶发 5xx/567 反爬瞬时错误，重试一次（间隔 1.5s）再放弃，
    避免一次抖动就把该源判死整个缓存窗口。
    """
    last: Exception | None = None
    for attempt in range(2):
        try:
            resp = await asyncio.wait_for(fetch(url, headers=headers), timeout=_FETCH_TIMEOUT)
            rows = parse(resp.text)
            return [r for r in rows if _in_window(r["ts"], minutes)], True, ""
        except Exception as exc:  # noqa: BLE001 - 单源失败不影响其他源
            last = exc
            if attempt == 0:
                await asyncio.sleep(1.5)  # 瞬时 5xx/反爬抖动间隔后重试一次
    log.info("热点源 %s 抓取失败：%s", name, last)
    return [], False, f"{type(last).__name__}: {last}"


async def _fetch_all(minutes: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results = await asyncio.gather(
        *[_fetch_one(name, url, headers, parse, minutes) for name, url, headers, parse in _FEEDS]
    )
    items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for (name, _url, _headers, _parse), (rows, ok, error) in zip(_FEEDS, results):
        sources.append({"name": name, "ok": ok, "count": len(rows), "error": error if not ok else ""})
        items.extend(rows)
    if not items and not any(s["ok"] for s in sources):
        raise ProviderError("全部热点快讯源均不可用")
    return items, sources


# 标题归一要去掉的字符：全角空格、中文/英文标点、括号、连字符（含英文引号 chr(34)/chr(39)）
_TITLE_STRIP = set(" \u3000，。！？、；：（）()【】[]·—-") | {chr(34), chr(39)}


def _title_fp(title: str) -> str:
    """标题指纹：去掉【】包裹/前缀与常见标点、空白，取前 24 字，用于跨源去重。

    同一条新闻在多个源标题略有差异（如新浪带【】包裹、东财多感叹号），
    归一后指纹一致即可合并；正文不同的新闻指纹不同，不会被误合并。
    """
    t = title or ""
    m = _RICH_RE.match(t)
    if m:
        # 【标题】整条包裹 → 取标题；【前缀】正文 → 取正文
        t = m.group(2) or m.group(1)
    t = "".join(ch for ch in t if ch not in _TITLE_STRIP and not ch.isspace())
    raw = t[:24].encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:16]


def _merge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按标题指纹去重（同一条新闻跨源重复），保留最新时间，再按时间倒序。"""
    best: dict[str, dict[str, Any]] = {}
    for it in items:
        fp = _title_fp(it["title"])
        cur = best.get(fp)
        if cur is None or it["ts"] > cur["ts"]:
            best[fp] = it
    ordered = sorted(best.values(), key=lambda x: x["ts"], reverse=True)
    return ordered[:HOTSPOT_LIMIT]


# ------------------------------------------------------------------ 组装入口

async def get_hotspot(minutes: int = HOTSPOT_MINUTES, force: bool = False) -> dict[str, Any]:
    """返回 {items, meta}。minutes 限定时间窗（5-120 分钟）。"""
    minutes = min(max(int(minutes), 5), 120)
    key = f"hotspot:{minutes}"
    try:
        return await cache.get_or_set(key, TTL, lambda: _load(minutes), force=force)
    except ProviderError as exc:
        result = {
            "items": [],
            "meta": {
                "error": f"热点获取失败：{exc}",
                "window_minutes": minutes,
                "total": 0,
                "fetched_at": now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }
        # 失败结果也短暂缓存：全部源故障期间反复请求不再重打外部快讯接口
        cache.put(key, result, min(TTL, 30.0))
        return result


async def _load(minutes: int) -> dict[str, Any]:
    items, sources = await _fetch_all(minutes)
    merged = _merge(items)
    # 统一输出字段：时间字符串（前端展示 HH:MM）+ 重点媒体标记
    for it in merged:
        it["time"] = datetime.fromtimestamp(it["ts"], TZ).strftime("%Y-%m-%d %H:%M:%S")
        it["media_badge"] = _is_hot_media(it["source"])
    return {
        "items": merged,
        "meta": {
            "window_minutes": minutes,
            "total": len(merged),
            "fetched_at": now().strftime("%Y-%m-%d %H:%M:%S"),
            "since": (now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),
            "sources": sources,
        },
    }
