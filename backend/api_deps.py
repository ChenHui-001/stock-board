"""api.py 的辅助层：缓存校验、AI 单飞锁、HTTP/数据整理 helper。

把这些从 api.py 拆出来是因为：
  1. api.py 本来就重（30KB+ 路由 + Pydantic 模型 + 业务逻辑），
     路由表的"声明"性质应该压倒"过程式 helper"；
  2. 缓存校验（_cached_report/_cache_fresh/_BLANK_LLM_REASON_RE/...）自成一体，
     与 AI 业务紧密耦合但与 HTTP 路由无关；
  3. AI 单飞锁（_with_ai_lock）独立可测。

为了向后兼容现有外部调用 `api._with_ai_lock(...)`、`api._cached_report(...)` 等，
api.py 在 `__all__` 之外仍 re-export 这些符号；本文件是单一来源（SSOT）。
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from . import llmcfg, scorecfg
from .storage import get_report, is_watched
from .utils import describe_exc, is_trading_now


# ============================================================ AI 缓存：新鲜度 + 作废

# AI 报告必含字段：升级后旧格式缓存缺新字段时自动作废（重新生成）
_REQUIRED_REPORT_FIELDS = ("report_sentiment", "rating_dist", "reports_preview")

# 历史缺陷缓存作废：旧版本在 LLM 超时类异常 str() 为空时，把「LLM 请求失败: 」
# 这种空白尾巴写进 degraded_reason 并入库。升级后这些缓存仍能通过下方各项校验
# 继续命中，用户会一直看到空白报错；检测到即作废重建（重新分析拿到新提示）。
_BLANK_LLM_REASON_RE = re.compile(r"LLM 请求失败:\s*[）)]")

# AI 报告结构版本：机会/风险条目升级为 {text,strength,hit,confidence} 后引入。
# 带版本号的缓存直接命中，不带的一律作废重建——比逐条结构检查更可靠
# （某些股票机会/风险恰好无盘口信号、全是字符串条目，也会被旧检查误判）。
REPORT_SCHEMA_VERSION = 3


def _cache_fresh(cached_at: str) -> bool:
    """AI 报告快照是否仍新鲜（盘中 120s / 盘后 1h，可配 AI_CACHE_TTL_*）。"""
    try:
        ts = datetime.strptime(cached_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False  # 解析失败（异常数据），宁可重建
    age = (datetime.now() - ts).total_seconds()
    ttl = _ai_cache_ttl_open() if is_trading_now() else _ai_cache_ttl_closed()
    return age <= ttl


def _ai_cache_ttl_open() -> float:
    from .config import settings
    return settings.AI_CACHE_TTL_OPEN


def _ai_cache_ttl_closed() -> float:
    from .config import settings
    return settings.AI_CACHE_TTL_CLOSED


def _cached_report(code: str) -> dict[str, Any] | None:
    """当日缓存命中；但 LLM 配置变化、字段缺失或快照过旧时旧缓存作废。

    时效：盘中超过 AI_CACHE_TTL_OPEN（默认 120s）即视为过期——点击 AI 分析
    时拿到的都是最新实时数据，避免命中几小时前的旧快照；刚分析完短时间内
    再点仍复用，防止对同一只股票重复打 LLM。盘后数据不变，放宽到 1 小时。
    """
    cached = get_report(code)
    if not cached:
        return None
    if not _cache_fresh(cached.get("cached_at") or ""):
        return None
    meta = cached.get("meta") or {}
    if meta.get("fingerprint") != llmcfg.fingerprint():
        return None
    if meta.get("score_fp") != scorecfg.fingerprint():
        return None
    if any(k not in cached for k in _REQUIRED_REPORT_FIELDS):
        return None
    adv_scores = ((cached.get("analysis") or {}).get("advice") or {}).get("scores") or {}
    if not adv_scores or "intraday" not in adv_scores:
        return None
    if (meta.get("schema_version") or 0) < REPORT_SCHEMA_VERSION:
        return None
    if _BLANK_LLM_REASON_RE.search(meta.get("degraded_reason") or ""):
        return None
    if meta.get("is_brief"):
        return None
    return cached


def _cached_brief_report(code: str) -> dict[str, Any] | None:
    """读取缓存，允许命中批量生成的轻量快照（is_brief=True）。"""
    cached = get_report(code)
    if not cached:
        return None
    if not _cache_fresh(cached.get("cached_at") or ""):
        return None
    meta = cached.get("meta") or {}
    if meta.get("fingerprint") != llmcfg.fingerprint():
        return None
    if meta.get("score_fp") != scorecfg.fingerprint():
        return None
    if any(k not in cached for k in _REQUIRED_REPORT_FIELDS):
        return None
    adv_scores = ((cached.get("analysis") or {}).get("advice") or {}).get("scores") or {}
    if not adv_scores or "intraday" not in adv_scores:
        return None
    if (meta.get("schema_version") or 0) < REPORT_SCHEMA_VERSION:
        return None
    if _BLANK_LLM_REASON_RE.search(meta.get("degraded_reason") or ""):
        return None
    return cached


# ============================================================ AI 单飞锁

class _AILock:
    """带等待计数的锁：计数归零即可从锁表摘除，避免锁表随 key 基数无限膨胀。"""

    __slots__ = ("lock", "waiters")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.waiters = 0


_ai_locks: dict[str, _AILock] = {}


async def _with_ai_lock(code: str, work: Callable[[], Awaitable[Any]]) -> Any:
    """同一股票并发触发分析时只执行一次 LLM 调用，其余请求等待并复用其结果。"""
    entry = _ai_locks.get(code)
    if entry is None:
        entry = _ai_locks[code] = _AILock()
    entry.waiters += 1
    try:
        async with entry.lock:
            return await work()
    finally:
        entry.waiters -= 1
        if entry.waiters <= 0 and _ai_locks.get(code) is entry:
            _ai_locks.pop(code, None)


# ============================================================ 路由层 helper

def _fail(exc: Exception, hint: str) -> HTTPException:
    """统一把 ProviderError 包成 503，前端可读 message。"""
    return HTTPException(status_code=503, detail=f"{hint}：{exc}")


def _sentiment_stats(items: list[dict[str, Any]]) -> dict[str, int]:
    """统计资讯/研报情绪分布与评分（与规则引擎口径一致：利好 +5 / 利空 -5 封顶 ±15）。"""
    bull = sum(1 for it in items if (it.get("interpretation") or {}).get("sentiment") == "利好")
    bear = sum(1 for it in items if (it.get("interpretation") or {}).get("sentiment") == "利空")
    neutral = len(items) - bull - bear
    return {
        "bull": bull,
        "bear": bear,
        "neutral": neutral,
        "score": max(-15, min(15, bull * 5 - bear * 5)),
    }


def _mark_value_watched(result: dict[str, Any]) -> None:
    """给选股结果补当前自选状态（pools 与 stocks 共用的股票对象）。"""
    for s in (result.get("stocks") or []):
        code = s.get("code")
        if code:
            s["watched"] = is_watched(code)
    for pool in (result.get("pools") or {}).values():
        for s in pool or []:
            code = s.get("code")
            if code:
                s["watched"] = is_watched(code)


def _ai_summary_from_report(report: dict[str, Any]) -> dict[str, Any]:
    """从完整/轻量 AI 报告中提取首页所需的轻量摘要。"""
    adv = (report.get("analysis") or {}).get("advice") or {}
    meta = report.get("meta") or {}
    return {
        "code": report.get("code"),
        "name": report.get("name"),
        "board": report.get("board"),
        "price": report.get("price"),
        "change_pct": report.get("change_pct"),
        "action": adv.get("action"),
        "confidence": adv.get("confidence"),
        "reason": adv.get("reason"),
        "confidence_reason": adv.get("confidence_reason"),
        "position": adv.get("position"),
        "support": adv.get("support"),
        "resistance": adv.get("resistance"),
        "entry_zone": adv.get("entry_zone"),
        "exit_zone": adv.get("exit_zone"),
        "stop_loss": adv.get("stop_loss"),
        "take_profit": adv.get("take_profit"),
        "horizon": adv.get("horizon"),
        "signal": adv.get("signal"),
        "signal_note": adv.get("signal_note"),
        "scores": adv.get("scores"),
        "engine": meta.get("engine") or "rule",
        "model": meta.get("model"),
        "cached_at": report.get("cached_at") or meta.get("generated_at"),
        "is_brief": report.get("is_brief") or meta.get("is_brief") or False,
    }


__all__ = [
    # AI 缓存
    "_REQUIRED_REPORT_FIELDS",
    "_BLANK_LLM_REASON_RE",
    "REPORT_SCHEMA_VERSION",
    "_cache_fresh",
    "_cached_report",
    "_cached_brief_report",
    # AI 单飞锁
    "_AILock",
    "_ai_locks",
    "_with_ai_lock",
    # 路由 helper
    "_fail",
    "_sentiment_stats",
    "_mark_value_watched",
    "_ai_summary_from_report",
]
