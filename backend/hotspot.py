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
from urllib.parse import urlencode

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

# 单源抓取超时（秒）：差异化分级，避免慢源拖累快源 / 总响应。
_TIMEOUT_BY_TIER = {
    "fast": settings.HOTSPOT_TIMEOUT_FAST,
    "normal": settings.HOTSPOT_TIMEOUT_NORMAL,
    "slow": settings.HOTSPOT_TIMEOUT_SLOW,
}


class SourceStat:
    """单源健康统计：连续失败次数 + 最近失败时间。

    - 连续失败 ≥ CIRCUIT_OPEN_AT：自动熔断，后续调用直接短路返回失败，不再打上游；
    - 熔断后静默 CIRCUIT_COOLDOWN 秒，期间所有调用继续短路；
    - 冷却到期后下一次调用重新尝试，恢复成功则重置计数。
    """

    def __init__(self, name: str, *, open_at: int, cooldown: float) -> None:
        self.name = name
        self.open_at = open_at
        self.cooldown = cooldown
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None

    def record_success(self) -> None:
        """成功后清零计数 + 关闭熔断。"""
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def record_failure(self) -> None:
        """失败累加；达到阈值时打熔断并打点。"""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.open_at and self._circuit_opened_at is None:
            import time as _time
            self._circuit_opened_at = _time.monotonic()
            log.warning("热点源 %s 连续失败 %d 次，触发熔断冷却 %.0fs",
                        self.name, self._consecutive_failures, self.cooldown)

    def is_open(self) -> bool:
        """是否处于熔断冷却中。冷却到期自动恢复（半开放）。"""
        if self._circuit_opened_at is None:
            return False
        import time as _time
        if _time.monotonic() - self._circuit_opened_at >= self.cooldown:
            # 冷却到期：放开一次尝试，成功则 record_success 自动清零；失败重新打熔断
            self._circuit_opened_at = None
            self._consecutive_failures = 0
            return False
        return True

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


# 模块级单源统计实例（进程内，不需要持久化）：避免重启前一直打故障源
_SOURCE_STATS: dict[str, SourceStat] = {}


def is_hot_media(source: str) -> bool:
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


# ------------------------------------------------------------------ 财联社电报（需签名）

_CLS_ROLL_PARAMS = {"app": "CailianpressWeb", "os": "web", "sv": "7.7.5", "rn": "50", "last_time": ""}
_CLS_LEAD_RE = re.compile(r"^财联社\d+月\d+日电[，,：:]?\s*")


def _cls_sign(params: dict[str, str]) -> str:
    """财联社公开电报接口签名：sha1(排序后 query) 的十六进制再 md5。"""
    q = urlencode(sorted(params.items()))
    return hashlib.md5(hashlib.sha1(q.encode()).hexdigest().encode()).hexdigest()


def _cls_url() -> str:
    """财联社电报列表完整 URL（含 sign）。参数固定，签名可预计算。"""
    sign = _cls_sign(_CLS_ROLL_PARAMS)
    return f"https://www.cls.cn/v1/roll/get_roll_list?{urlencode({**_CLS_ROLL_PARAMS, 'sign': sign})}"


def _split_cls_content(content: str) -> tuple[str, str]:
    """财联社电报正文：去掉「财联社X月X日电，」电头后取首句为标题。"""
    body = _CLS_LEAD_RE.sub("", content or "").strip()
    return _split_wscn(body)


def _parse_cls(text: str) -> list[dict[str, Any]]:
    """财联社电报：{errno, data:{roll_data:[{id,content,ctime(秒),brief,shareurl}]}}"""
    try:
        payload = json.loads(text)
        rows = (payload.get("data") or {}).get("roll_data") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = str(row.get("content") or row.get("brief") or "").strip()
        ts = _to_ts(row.get("ctime"))
        if not content or ts is None:
            continue
        title, summary = _split_cls_content(content)
        if not title:
            continue
        out.append({
            "id": str(row.get("id") or ""),
            "title": title,
            "summary": summary,
            "ts": ts,
            "source": "财联社",
            "origin": "财联社",
            "url": str(row.get("shareurl") or "").strip(),
        })
    return out


# ------------------------------------------------------------------ 金十数据快讯

_JIN10_LEAD_RE = re.compile(r"^金十数据\d+月\d+日[讯，,：:]?\s*")


def _split_jin10_content(content: str) -> tuple[str, str]:
    """金十快讯正文：优先拆【标题】摘要并去掉电头；无【】时取首句为标题。"""
    m = _RICH_RE.match(content or "")
    if m:
        return m.group(1).strip(), _JIN10_LEAD_RE.sub("", m.group(2)).strip()
    return _split_wscn(content)


def _parse_jin10(text: str) -> list[dict[str, Any]]:
    """金十数据快讯：{status, data:[{id,data:{content},time('YYYY-MM-DD HH:MM:SS')}]}"""
    try:
        payload = json.loads(text)
        rows = payload.get("data") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = str((row.get("data") or {}).get("content") or "").strip()
        ts = _parse_dt(str(row.get("time") or ""))
        if not content or ts is None:
            continue
        title, summary = _split_jin10_content(content)
        if not title:
            continue
        out.append({
            "id": str(row.get("id") or ""),
            "title": title,
            "summary": summary,
            "ts": int(ts.timestamp()),
            "source": "金十数据",
            "origin": "金十数据",
            "url": "",
        })
    return out


# ------------------------------------------------------------------ 抓取与合并

# 每条：name, url, headers, parse, tier。tier 决定单源超时（fast/normal/slow）。
# 同花顺/新浪/华尔街见闻：长期稳定 4s；东财/财联社：偶发 5xx 给 6s；
# 金十数据：首次冷启动偶发 3s，给 10s 容错。
_FEEDS: list[tuple[str, str, dict[str, str], Any, str]] = [
    ("同花顺", "https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize=50",
     {"Referer": "https://news.10jqka.com.cn/"}, _parse_ths, "fast"),
    ("东方财富", "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col&column=345&order=1&needInteractData=0&page_index=1&page_size=50&req_trace=hotspot",
     {"Referer": "https://finance.eastmoney.com/"}, _parse_em, "normal"),
    ("新浪财经", "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=50&zhibo_id=152&tag_id=0&dire=f&dpc=1",
     {"Referer": "https://finance.sina.com.cn/"}, _parse_sina, "fast"),
    ("华尔街见闻", "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=50",
     {"Referer": "https://wallstreetcn.com/"}, _parse_wscn, "fast"),
    ("财联社", _cls_url(), {"Referer": "https://www.cls.cn/telegraph"}, _parse_cls, "normal"),
    ("金十数据", "https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1",
     {"Referer": "https://www.jin10.com/", "x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0"}, _parse_jin10, "slow"),
]


async def _fetch_one(
    name: str, url: str, headers: dict[str, str], parse: Any, minutes: int,
    timeout: float, retry_backoffs: tuple[float, ...] = (1.0, 2.0),
    source_stats: dict[str, "SourceStat"] | None = None,
) -> tuple[list[dict[str, Any]], bool, str]:
    """抓取并解析单个源，只保留时间窗内的条目。失败返回 ([]，False, 原因)。

    东财等源偶发 5xx/567 反爬瞬时错误，按 `retry_backoffs` 序列做指数 backoff 重试，
    默认首次失败等 1s、第二次失败等 2s，避免一次抖动就把该源判死整个缓存窗口，
    同时防止双源同时抖动时一起重试挤占整体预算。
    """
    last: Exception | None = None
    attempts = len(retry_backoffs) + 1
    for attempt in range(attempts):
        try:
            resp = await asyncio.wait_for(fetch(url, headers=headers), timeout=timeout)
            rows = parse(resp.text)
            if source_stats is not None and name in source_stats:
                source_stats[name].record_success()
            return [r for r in rows if _in_window(r["ts"], minutes)], True, ""
        except Exception as exc:  # noqa: BLE001 - 单源失败不影响其他源
            last = exc
            if attempt < attempts - 1:
                await asyncio.sleep(retry_backoffs[attempt])  # 指数 backoff：1s → 2s
    if source_stats is not None and name in source_stats:
        source_stats[name].record_failure()
    log.info("热点源 %s 抓取失败：%s", name, last)
    return [], False, f"{type(last).__name__}: {last}"


async def _fetch_all(minutes: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # 用 asyncio.wait_for 包裹 gather 实现整体预算：超过 HOTSPOT_BUDGET 秒的慢源
    # 会被取消，避免一个挂掉的源把整个响应拖到源超时之和（4+6+4+4+6+10=34s）。
    budget = settings.HOTSPOT_BUDGET
    # 确保每个源都有一个 SourceStat 实例（按需懒创建，配置来自 settings）
    stats: dict[str, SourceStat] = {}
    for name, *_ in _FEEDS:
        if name not in _SOURCE_STATS:
            _SOURCE_STATS[name] = SourceStat(
                name,
                open_at=settings.HOTSPOT_CIRCUIT_OPEN_AT,
                cooldown=settings.HOTSPOT_CIRCUIT_COOLDOWN,
            )
        stats[name] = _SOURCE_STATS[name]

    # 熔断中的源直接短路返回，不打上游、也不占预算
    short_circuit: list[tuple[str, bool, str]] = []  # (name, ok, error)
    to_fetch: list[tuple[Any, ...]] = []
    for feed in _FEEDS:
        name = feed[0]
        if stats[name].is_open():
            short_circuit.append((name, False, "circuit_open"))
        else:
            to_fetch.append(feed)

    tasks = [
        asyncio.create_task(_fetch_one(
            name, url, headers, parse, minutes,
            timeout=_TIMEOUT_BY_TIER.get(tier, _TIMEOUT_BY_TIER["normal"]),
            retry_backoffs=(1.0, 2.0),
            source_stats=stats,
        ))
        for name, url, headers, parse, tier in to_fetch
    ]
    fetched_results: list[tuple[list[dict[str, Any]], bool, str]] = []
    if tasks:
        try:
            fetched_results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=False), timeout=budget,
            )
        except asyncio.TimeoutError:
            # 预算超时：取消所有未完成的协程，记录哪些源未返回
            pending = [t for t in tasks if not t.done()]
            for t in pending:
                t.cancel()
            # 已完成的取结果，未完成的记 timeout；超时也算一次失败，触发熔断计数
            for feed, t in zip(to_fetch, tasks):
                name = feed[0]
                if t.done() and not t.cancelled() and t.exception() is None:
                    fetched_results.append(t.result())
                else:
                    fetched_results.append(([], False, "timeout"))
                    stats[name].record_failure()
            log.warning("热点聚合超出预算 %.1fs，%d 个源被截断", budget, len(pending))

    # 把熔断短路结果与抓取结果按 _FEEDS 顺序拼回去，保证 sources 列表对齐
    fetched_iter = iter(fetched_results)
    results: list[tuple[list[dict[str, Any]], bool, str]] = []
    for feed in _FEEDS:
        name = feed[0]
        if stats[name].is_open():
            results.append(([], False, "circuit_open"))
        else:
            try:
                results.append(next(fetched_iter))
            except StopIteration:
                results.append(([], False, "unknown"))
    # 把先记录的 short_circuit 也并入（理论上 is_open 已覆盖）
    _ = short_circuit  # 保持语义清晰
    items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for (name, _url, _headers, _parse, _tier), (rows, ok, error) in zip(_FEEDS, results):
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
        it["media_badge"] = is_hot_media(it["source"])
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
