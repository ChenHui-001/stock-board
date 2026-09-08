"""市场热点追踪：聚合多源 7x24 快讯，展示近 N 分钟内热点资讯。

数据源均为公开网页/接口加载的真实数据（非模拟）：
- 同花顺 7x24 快讯  news.10jqka.com.cn/tapp/news/push/stock/
- 东方财富 快讯    np-listapi.eastmoney.com/comm/web/getNewsByColumns
- 新浪财经 7x24    zhibo.sina.com.cn/api/zhibo/feed
- 华尔街见闻 7x24  api-one.wallstcn.com/apiv1/content/lives

各源并行抓取 → 统一成条目 → 按时间窗过滤 → 双条件去重（标题指纹精确 +
bigram Dice 相似兜底）→ 按时间倒序。概念标签经标签引擎（标题×3/泛词门槛/
父子去重/标签级情绪）产出，概念热度按发酵强度模型（时效半衰期 + 情绪/信源
加权 + 子窗 log 斜率趋势）排序，见模块顶部 HEAT_* 常量区。
媒体署名（彭博社/财联社/财新/澎湃等）保留在 source 字段，命中《重点媒体》
名单的条目加 media_badge 标记，便于前端优先突出展示。

结果整体进内存缓存（默认 90s），避免反复打外部快讯接口。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from .cache import cache
from .config import settings
from .hotspot_ai import _SECTOR_DICT, _tag_sentiment
from .providers.base import ProviderError, fetch
from .utils import TZ, now
from . import metrics, value_screener

log = logging.getLogger("hotspot")

HOTSPOT_MINUTES = settings.HOTSPOT_MINUTES  # 默认时间窗（分钟，环境变量可配）
HOTSPOT_LIMIT = 40        # 单次最多返回条数
TTL = settings.HOTSPOT_TTL  # 聚合结果缓存（秒，环境变量可配）

# ------------------------------------------------------------------ HEAT_* 发酵强度模型常量区
# 热度/趋势/去重全部可调参数集中此处，调参只改这里（约定：一律 HEAT_ 前缀）。
# 回滚开关：HEAT_SIM_TH > 1 → 关闭相似度合并（条件B）；HEAT_SLOPE_W = 0 → 退化为纯热度排序。
HEAT_HALF_LIFE_S = 600.0   # 时效半衰期（秒）：10 分钟前的提及权重减半
HEAT_TITLE_W = 3.0         # 标题命中权重（摘要命中 ×1）
HEAT_EMO_W = {"利好": 1.2, "中性": 1.0, "利空": 0.8}          # 标签级情绪权重
# 信源权重（以 _FEEDS 实际源名为准；架构师默认值：财联社 1.3 / 华尔街见闻 1.2 /
# 新浪·东财 1.0 / 其余 0.8。同花顺与新浪/东财同为一线 7x24 源，按 1.0 计。
HEAT_SOURCE_W: dict[str, float] = {
    "财联社": 1.3, "华尔街见闻": 1.2,
    "同花顺": 1.0, "东方财富": 1.0, "新浪财经": 1.0,
    "金十数据": 0.8,
}
HEAT_SOURCE_W_DEFAULT = 0.8  # 未登记信源（如全网检索来源）的缺省权重
HEAT_SLOPE_K = 4            # 斜率子窗数：窗口均分 K 份做 log 线性回归
HEAT_TREND_TH = 0.15        # trend 判定阈值：slope ≥ +0.15 up / ≤ −0.15 down / 其余 flat
HEAT_SLOPE_W = 0.5          # rank_score 中斜率项权重；置 0 退化为纯热度排序
HEAT_SIM_TH = 0.62          # 标题 bigram Dice 相似度合并阈值（>1 即关闭条件B）
HEAT_SENT_WINDOW = 12       # 标签级情绪邻近窗口（命中词前后字数），传入 _tag_sentiment

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
        # _SOURCE_STATS 是进程级共享状态，FastAPI 异步并发下同一源可能被多协程
        # 同时调用 record_success/failure。读改写（如 += 1）非原子，需要串行化。
        # 锁开销 < 1µs，且修改频率极低（每分钟最多几次），可忽略。
        self._lock = asyncio.Lock()

    async def record_success(self) -> None:
        """成功后清零计数 + 关闭熔断。async + 锁：与并发 record_failure 串行化。"""
        async with self._lock:
            self._consecutive_failures = 0
            self._circuit_opened_at = None
        self._sync_metrics()

    async def record_failure(self) -> None:
        """失败累加；达到阈值时打熔断并打点。async + 锁：读改写 ( += 1)非原子。"""
        async with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.open_at and self._circuit_opened_at is None:
                self._circuit_opened_at = time.monotonic()
                log.warning("热点源 %s 连续失败 %d 次，触发熔断冷却 %.0fs",
                            self.name, self._consecutive_failures, self.cooldown)
        self._sync_metrics()

    async def is_open(self) -> bool:
        """是否处于熔断冷却中。冷却到期自动恢复（半开放）。

        注：查询时有副作用（冷却到期会重置状态）。这是有意为之——`is_open()`
        实际上等同于「我该不该短路」，冷却到期返回 False 等同于「这次放行」。
        async + 锁：与并发 record_* 串行化，避免读到半修改状态。
        """
        async with self._lock:
            if self._circuit_opened_at is None:
                result = False
            elif time.monotonic() - self._circuit_opened_at >= self.cooldown:
                # 冷却到期：放开一次尝试（半开放），成功则 record_success 自动清零；失败重新打熔断。
                self._reset()
                result = False
            else:
                result = True
        # 同步 metric 在锁外（写入 Gauge 本身不需串行化，且 metrics 内部无锁）
        metrics.update_source_gauge(self.name, self._consecutive_failures, result)
        return result

    def _reset(self) -> None:
        """熔断状态完全清零（冷却到期时由 is_open 调用，外部不应直接调用）。"""
        self._circuit_opened_at = None
        self._consecutive_failures = 0

    def _sync_metrics(self) -> None:
        """把当前 _consecutive_failures 与熔断状态同步到 Prometheus Gauge。

        在 record_success/record_failure 末尾调用，无需等下次 is_open 才能反映到指标。
        """
        circuit_open = self._circuit_opened_at is not None
        metrics.update_source_gauge(self.name, self._consecutive_failures, circuit_open)

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
    start_ts = metrics.now_ts()  # 起点：用于直方图统计单源耗时
    for attempt in range(attempts):
        try:
            resp = await asyncio.wait_for(fetch(url, headers=headers), timeout=timeout)
            rows = parse(resp.text)
            if source_stats is not None and name in source_stats:
                await source_stats[name].record_success()
            elapsed = metrics.now_ts() - start_ts
            metrics.observe_duration(name, "success", elapsed)
            metrics.SOURCE_REQUESTS.labels(source=name, result="success").inc()
            metrics.SOURCE_ITEMS.labels(source=name).inc(len(rows))
            return [r for r in rows if _in_window(r["ts"], minutes)], True, ""
        except asyncio.TimeoutError:
            last = asyncio.TimeoutError("单源抓取超时")
            if attempt < attempts - 1:
                await asyncio.sleep(retry_backoffs[attempt])  # 指数 backoff：1s → 2s
        except Exception as exc:  # noqa: BLE001 - 单源失败不影响其他源
            last = exc
            if attempt < attempts - 1:
                await asyncio.sleep(retry_backoffs[attempt])  # 指数 backoff：1s → 2s
    if source_stats is not None and name in source_stats:
        await source_stats[name].record_failure()
    # 区分超时 vs 其他错误，便于按 result 标签切片
    elapsed = metrics.now_ts() - start_ts
    if isinstance(last, asyncio.TimeoutError):
        result_label = "timeout"
    else:
        result_label = "error"
    metrics.observe_duration(name, result_label, elapsed)
    metrics.SOURCE_REQUESTS.labels(source=name, result=result_label).inc()
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
        if await stats[name].is_open():
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
                    await stats[name].record_failure()
            log.warning("热点聚合超出预算 %.1fs，%d 个源被截断", budget, len(pending))

    # 把熔断短路结果与抓取结果按 _FEEDS 顺序拼回去，保证 sources 列表对齐
    fetched_iter = iter(fetched_results)
    results: list[tuple[list[dict[str, Any]], bool, str]] = []
    for feed in _FEEDS:
        name = feed[0]
        if await stats[name].is_open():
            results.append(([], False, "circuit_open"))
            metrics.SOURCE_REQUESTS.labels(source=name, result="circuit_open").inc()
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
    # 聚合摘要日志：仅在有失败时输出 INFO，正常成功路径走 DEBUG 不刷屏；
    # 一次性看到「6 源耗时结果 + 各自条数 + 失败原因」，排障不用再翻 6 个 warn。
    failed = [s for s in sources if not s["ok"]]
    total_count = sum(s["count"] for s in sources)
    if failed:
        log.info("热点聚合完成 %d/%d 源成功，共 %d 条；失败：%s",
                 len(sources) - len(failed), len(sources), total_count,
                 "; ".join(f"{f['name']}={f['error']}" for f in failed))
    else:
        log.debug("热点聚合完成 %d 源成功，共 %d 条", len(sources), total_count)
    if not items and not any(s["ok"] for s in sources):
        raise ProviderError("全部热点快讯源均不可用")
    return items, sources


# 标题归一要去掉的字符：全角空格、中文/英文标点、括号、连字符（含英文引号 chr(34)/chr(39)）
_TITLE_STRIP = set(" \u3000，。！？、；：（）()【】[]·—-") | {chr(34), chr(39)}


def _norm_title(title: str) -> str:
    """标题归一：去掉【】包裹/前缀与常见标点、空白（与 _title_fp 同一口径）。"""
    t = title or ""
    m = _RICH_RE.match(t)
    if m:
        # 【标题】整条包裹 → 取标题；【前缀】正文 → 取正文
        t = m.group(2) or m.group(1)
    return "".join(ch for ch in t if ch not in _TITLE_STRIP and not ch.isspace())


def _title_fp(title: str) -> str:
    """标题指纹：归一后取前 24 字 md5，用于跨源精确去重（条件A）。

    同一条新闻在多个源标题略有差异（如新浪带【】包裹、东财多感叹号），
    归一后指纹一致即可合并；正文不同的新闻指纹不同，不会被误合并。
    """
    return hashlib.md5(_norm_title(title)[:24].encode("utf-8")).hexdigest()[:16]


def _title_similar(a: str, b: str) -> float:
    """标题相似度：字符 bigram Dice 系数（改写式标题兜底，条件B）。

    不用编辑距离：O(L²)/对在大文本上浪费；Dice 对中文短标题区分度足够且
    O(L)/对。空串/单字无 bigram 时返回 0（宁漏合不误合）。
    """
    def grams(t: str) -> set[str]:
        t = _norm_title(t)
        if len(t) < 2:
            return {t} if t else set()
        return {t[i:i + 2] for i in range(len(t) - 1)}

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return 2.0 * inter / (len(ga) + len(gb))


def _merge(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """双条件去重合并（同一条新闻跨源/改写式重复），保留最新时间，按时间倒序。

    条件A（精确）：_title_fp 前 24 字指纹相等即合并；
    条件B（相似）：标题 bigram Dice ≥ HEAT_SIM_TH 即合并，按归一标题首 8 字
    分桶、仅桶内两两比较（全量 O(n²) → 桶内近似线性）。
    合并保留 ts 最新的代表条目，被合并条数记入代表条目 ``dups`` 字段
    （仅记录可观测，不进热度公式）。HEAT_SIM_TH > 1 时关闭条件B（回滚开关）。
    """
    # ---- 条件A：指纹精确合并
    best: dict[str, dict[str, Any]] = {}
    fp_counts: dict[str, int] = {}
    for it in items:
        fp = _title_fp(it["title"])
        fp_counts[fp] = fp_counts.get(fp, 0) + 1
        cur = best.get(fp)
        if cur is None or it["ts"] > cur["ts"]:
            best[fp] = it
    reps = list(best.values())
    for r in reps:
        # 同指纹被合并掉条数（不含代表自身）
        r["dups"] = fp_counts[_title_fp(r["title"])] - 1

    # ---- 条件B：首 8 字分桶 + 桶内 Dice 相似合并
    kept_ids: set[int] = {id(r) for r in reps}
    if HEAT_SIM_TH <= 1.0:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in reps:
            buckets.setdefault(_norm_title(r["title"])[:8], []).append(r)
        for group in buckets.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda r: r["ts"], reverse=True)
            anchors: list[dict[str, Any]] = []
            for cand in group:
                placed = False
                for a in anchors:
                    if _title_similar(a["title"], cand["title"]) >= HEAT_SIM_TH:
                        a["dups"] += 1 + cand.get("dups", 0)
                        kept_ids.discard(id(cand))
                        placed = True
                        break
                if not placed:
                    anchors.append(cand)

    ordered = sorted(
        (r for r in reps if id(r) in kept_ids),
        key=lambda x: x["ts"], reverse=True,
    )
    return ordered[:HOTSPOT_LIMIT]


def _extract_item_tags(title: str, summary: str) -> list[dict[str, Any]]:
    """为单条快讯打概念标签（标签引擎重写版）。

    匹配：遍历 _SECTOR_DICT 全量多模式子串匹配（纯 str.find，≤200 条毫秒级）。
    - 标题命中 ×HEAT_TITLE_W，摘要命中 ×1；同一概念双命中取大；
    - 泛词（generic 登记）仅在标题命中时才计分（泛词门槛）；
    - 父子去重：命中子概念时父概念不计数，除非父有独立核心词（非泛词、
      且不与已命中子概念共享关键词）命中；
    - 标签级情绪：_tag_sentiment 取命中词邻近窗口判定（LLM 扩展点）。

    输出结构：[{"name", "sentiment", "score", "hit", "src"}]，
    src ∈ "title" | "summary" | "title+summary"（最优命中的来源）。
    """
    title_s = title or ""
    summary_s = summary or ""

    # 第一遍：全量匹配，每个概念记录最优命中与全部命中词
    raw: dict[str, dict[str, Any]] = {}
    for name, cfg in _SECTOR_DICT.items():
        best: dict[str, Any] | None = None
        hits: list[str] = []
        for kw in cfg["keywords"]:
            in_title = kw in title_s
            in_summary = kw in summary_s
            if not (in_title or in_summary):
                continue
            if cfg["generic"].get(kw) and not in_title:
                continue  # 泛词门槛：仅标题命中才计分
            hits.append(kw)
            weight = HEAT_TITLE_W if in_title else 1.0
            src = "title+summary" if (in_title and in_summary) else ("title" if in_title else "summary")
            if best is None or weight > best["score"]:
                best = {"score": weight, "hit": kw, "src": src}
        if best is not None:
            best["hits"] = hits
            raw[name] = best

    # 第二遍：父子去重。子概念命中后，父概念仅在有「独立核心词」命中时保留。
    # 独立核心词 = 父的命中词既不是泛词，也不出现在任何已命中子概念的关键词表里。
    child_kws: dict[str, set[str]] = {}
    for cname, cinfo in raw.items():
        parent = _SECTOR_DICT[cname]["parent"]
        if parent:
            child_kws.setdefault(parent, set()).update(_SECTOR_DICT[cname]["keywords"])

    tags: list[dict[str, Any]] = []
    for name, info in raw.items():
        cfg = _SECTOR_DICT[name]
        if name in child_kws:
            # 本概念是某个已命中子概念的父：仅独立核心词命中才保留
            independent = [
                kw for kw in info["hits"]
                if not cfg["generic"].get(kw) and kw not in child_kws[name]
            ]
            if not independent:
                continue
        sentiment = _tag_sentiment(
            title_s, summary_s, info["hit"], ctx={"window": HEAT_SENT_WINDOW}
        )
        tags.append({
            "name": name,
            "sentiment": sentiment,
            "score": round(float(info["score"]), 3),
            "hit": info["hit"],
            "src": info["src"],
        })
    return tags


def _compute_sector_heat(
    items: list[dict[str, Any]], minutes: int, now_ts: int
) -> list[dict[str, Any]]:
    """发酵强度分 + 斜率趋势（重写版）。

    fresh(t) = 2^(−(now−t)/HEAT_HALF_LIFE_S)：半衰期 600s 的时效衰减；
    w_item = tag.score × fresh × HEAT_EMO_W[情绪] × HEAT_SOURCE_W[信源]；
    heat = Σ w_item；heat_norm = 100 × heat / max（相对归一，抗绝对值漂移）；
    slope：窗口均分 HEAT_SLOPE_K 子窗各得 w_k，对 log(w_k+1) 最小二乘斜率，
           除以全表最大 |斜率| 归一到 [-1, 1]；
    trend：slope ≥ HEAT_TREND_TH → up / ≤ −TH → down / 其余 flat；
    rank_score = heat_norm × (1 + HEAT_SLOPE_W × slope)，按其降序输出。
    """
    window_s = max(minutes * 60, 1)
    window_start = now_ts - window_s
    k = HEAT_SLOPE_K
    sub = window_s / k
    half = window_s // 2
    recent_ts = now_ts - half

    agg: dict[str, dict[str, Any]] = {}
    for it in items:
        ts = it["ts"]
        age = max(now_ts - ts, 0)
        fresh = 2.0 ** (-age / HEAT_HALF_LIFE_S)
        src_w = HEAT_SOURCE_W.get(
            (it.get("source") or it.get("origin") or "").strip(),
            HEAT_SOURCE_W_DEFAULT,
        )
        for tag in (it.get("tags") or []):
            name = str(tag.get("name") or "").strip()
            if not name:
                continue
            sentiment = tag.get("sentiment", "中性")
            score = float(tag.get("score", 1.0) or 1.0)
            w = score * fresh * HEAT_EMO_W.get(sentiment, 1.0) * src_w
            b = agg.setdefault(name, {
                "total": 0, "bull": 0, "bear": 0, "neutral": 0,
                "recent": 0, "older": 0, "w": 0.0, "wsub": [0.0] * k,
            })
            b["total"] += 1
            if sentiment == "利好":
                b["bull"] += 1
            elif sentiment == "利空":
                b["bear"] += 1
            else:
                b["neutral"] += 1
            if ts >= recent_ts:
                b["recent"] += 1
            elif ts >= window_start:
                b["older"] += 1
            b["w"] += w
            # 子窗归属：越界（窗口外残留/时间漂移）夹到首尾子窗
            idx = min(k - 1, max(0, int((ts - window_start) // sub)))
            b["wsub"][idx] += w

    # 全表最大 |斜率|，用于把 slope 归一到 [-1, 1]
    slopes: dict[str, float] = {}
    max_abs = 0.0
    xs = list(range(k))
    x_mean = (k - 1) / 2.0
    y_den = sum((x - x_mean) ** 2 for x in xs) or 1.0
    for name, b in agg.items():
        ys = [math.log(w + 1.0) for w in b["wsub"]]
        y_mean = sum(ys) / k
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / y_den
        slopes[name] = slope
        max_abs = max(max_abs, abs(slope))

    max_heat = max((b["w"] for b in agg.values()), default=0.0)
    out: list[dict[str, Any]] = []
    for name, b in agg.items():
        slope_norm = slopes[name] / max_abs if max_abs > 0 else 0.0
        heat = b["w"]
        heat_norm = 100.0 * heat / max_heat if max_heat > 0 else 0.0
        if slope_norm >= HEAT_TREND_TH:
            trend = "up"
        elif slope_norm <= -HEAT_TREND_TH:
            trend = "down"
        else:
            trend = "flat"
        rank_score = heat_norm * (1.0 + HEAT_SLOPE_W * slope_norm)
        out.append({
            "name": name,
            "total": b["total"],
            "bull": b["bull"],
            "bear": b["bear"],
            "neutral": b["neutral"],
            "trend": trend,
            "recent": b["recent"],
            "older": b["older"],
            "heat": round(heat, 4),
            "heat_norm": round(heat_norm, 2),
            "slope": round(slope_norm, 4),
            "rank_score": round(rank_score, 2),
        })
    out.sort(key=lambda x: x["rank_score"], reverse=True)
    return out


# ------------------------------------------------------------------ 组装入口

async def get_hotspot(minutes: int = HOTSPOT_MINUTES, force: bool = False) -> dict[str, Any]:
    """返回 {items, meta}。minutes 限定时间窗（5-120 分钟）。"""
    minutes = min(max(int(minutes), 5), 120)
    key = f"hotspot:{minutes}"
    try:
        return await cache.get_or_set(key, TTL, lambda: _load(minutes), force=force)
    except ProviderError as exc:
        log.debug("%s 降级: %s", "get_hotspot", exc)
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
    now_ts = int(now().timestamp())
    # 统一输出字段：时间字符串（前端展示 HH:MM）+ 重点媒体标记 + 概念标签
    for it in merged:
        it["time"] = datetime.fromtimestamp(it["ts"], TZ).strftime("%Y-%m-%d %H:%M:%S")
        it["media_badge"] = is_hot_media(it["source"])
        it["tags"] = _extract_item_tags(it["title"], it.get("summary", ""))
    sector_heat = _compute_sector_heat(merged, minutes, now_ts)
    # 板块资金流龙头（P1-2 下钻）：独立请求路径 + 上游静默容错，不在热度热路径
    try:
        leaders = await value_screener.board_flow_leaders(limit=50)
    except Exception as exc:  # noqa: BLE001 - leaders 失败不阻塞主聚合
        log.info("热点板块龙头下钻数据获取失败（忽略）：%s", exc)
        leaders = []
    return {
        "items": merged,
        "meta": {
            "window_minutes": minutes,
            "total": len(merged),
            "fetched_at": now().strftime("%Y-%m-%d %H:%M:%S"),
            "since": (now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),
            "sources": sources,
            "sector_heat": sector_heat,
            "leaders": leaders,
        },
    }
