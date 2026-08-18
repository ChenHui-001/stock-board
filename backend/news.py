"""个股资讯：抓取近一个月相关新闻，逐条附 AI 解读。

解读走两条路径：
- LLM 可用：一次批量调用对全部条目输出情绪/影响/解读（结构化 JSON）。
- LLM 不可用或失败：关键词规则引擎兜底，同样输出 sentiment/impact/summary。

结果整体进内存缓存（原始资讯 10 分钟 + 解读 30 分钟），避免反复打数据源与 LLM。
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from . import llm
from .cache import cache
from .providers import ProviderError, registry
from .utils import now, resolve_market

log = logging.getLogger("news")

# 抓取窗口（天）
NEWS_DAYS = 30
# 单次最多展示并解读的条数（LLM 解读是批量一次调用，条数多会拖慢且耗 token）
NEWS_LIMIT = 12
# 原始资讯缓存（秒）
RAW_TTL = 600.0
# 解读缓存（秒）：资讯本身没变时解读可复用更久
INTERP_TTL = 1800.0

# ------------------------------------------------------------------ 规则解读
# 关键词 -> 情绪。标题/摘要命中任一即计入对应情绪；同时命中利好与利空时取数量多的一方。
_BULL_WORDS = [
    "涨停", "中标", "签订", "合同", "订单", "回购", "增持", "业绩预增", "净利润增长",
    "扭亏", "获批", "核准", "收购", "重组", "分红", "送转", "突破", "创新高", "超预期",
    "涨价", "扩产", "合作", "战略", "获批", "落地", "涨停板", "利好", "大涨", "签约",
    "中标金额", "同比增", "环比增", "翻倍", "创历史新高",
]
_BEAR_WORDS = [
    "跌停", "减持", "亏损", "预亏", "处罚", "立案", "调查", "违规", "退市", "风险警示",
    "解禁", "诉讼", "担保", "质押", "债务", "违约", "下调", "评级下调", "商誉减值",
    "利空", "大跌", "爆雷", "立案调查", "ST", "退市风险", "冻结", "罚款", "警示函",
]
# 高强度词 -> 影响程度高
_HIGH_IMPACT = [
    "重大", "重磅", "紧急", "立案", "退市", "重组", "收购", "爆雷", "处罚", "诉讼",
    "涨停", "跌停", "超预期", "业绩预增", "预亏", "创新高", "大跌", "大涨",
]


def _match_count(text: str, words: list[str]) -> int:
    return sum(1 for w in words if w in text)


def rule_interpret(item: dict[str, Any]) -> dict[str, Any]:
    """关键词规则解读单条资讯（LLM 不可用/失败时的兜底）。"""
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    bull, bear = _match_count(text, _BULL_WORDS), _match_count(text, _BEAR_WORDS)
    if bull > bear and bull > 0:
        sentiment = "利好"
    elif bear > bull and bear > 0:
        sentiment = "利空"
    else:
        sentiment = "中性"

    if _match_count(text, _HIGH_IMPACT) > 0:
        impact = "高"
    elif sentiment == "中性":
        impact = "低"
    else:
        impact = "中"

    words = [w for w in _BULL_WORDS + _BEAR_WORDS if w in text][:3]
    if sentiment == "利好":
        summary = (
            f"出现积极信号（{'、'.join(words) or '利好消息'}），可能对股价形成支撑，"
            "建议关注后续落地情况与持续性。"
        )
    elif sentiment == "利空":
        summary = (
            f"出现负面信号（{'、'.join(words) or '利空消息'}），可能压制股价，"
            "注意短期回调风险。"
        )
    else:
        summary = "中性资讯，未发现明确利好或利空信号，对股价直接影响有限。"
    return {"sentiment": sentiment, "impact": impact, "summary": summary, "engine": "rule"}


# ------------------------------------------------------------------ LLM 解读
_INTERP_SYSTEM = """你是一名 A 股财经资讯解读助手。你会收到某只股票近一个月的 N 条资讯（JSON 数组）。

请对每一条资讯给出解读，用于辅助该股的持仓决策。要求：
1. 只输出一个 JSON 对象，结构为 {"items": [{"idx": 0, "sentiment": "利好", "impact": "中", "summary": "..."}]}，不要输出任何其他文字或代码块。
2. sentiment 只取三选一：利好 / 利空 / 中性。
3. impact 只取三选一：高 / 中 / 低。
4. summary 为 40 字以内的一句话解读，说明该消息对股价的可能影响与关注要点。
5. idx 必须对应输入数组的下标，逐条输出，不要漏条，也不要新增不存在的条目。
"""


async def _llm_interpret(
    items: list[dict[str, Any]], code: str, name: str
) -> list[dict[str, Any]] | None:
    """批量解读。成功返回与 items 等长的解读列表，失败返回 None（由调用方回退规则）。"""
    if not llm.available():
        return None
    payload = [
        {
            "idx": i,
            "日期": item.get("date", "")[:10],
            "来源": item.get("source", ""),
            "标题": item.get("title", ""),
            "摘要": (item.get("summary", "") or "")[:120],
        }
        for i, item in enumerate(items)
    ]
    user = (
        f"以下是 {name}({code}) 近一个月的 {len(payload)} 条资讯，请逐条解读：\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=1)}"
    )
    try:
        raw, meta = await llm.chat_json(_INTERP_SYSTEM, user)
        rows = raw.get("items") or []
        if not isinstance(rows, list):
            return None
        out: list[dict[str, Any]] = []
        by_idx: dict[int, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row.get("idx", -1))
            except (TypeError, ValueError):
                continue
            by_idx[idx] = row
        for i in range(len(items)):
            row = by_idx.get(i) or {}
            sentiment = str(row.get("sentiment") or "")
            if sentiment not in ("利好", "利空", "中性"):
                sentiment = "中性"
            impact = str(row.get("impact") or "")
            if impact not in ("高", "中", "低"):
                impact = "中"
            summary = str(row.get("summary") or "").strip() or "暂无解读"
            out.append(
                {
                    "sentiment": sentiment,
                    "impact": impact,
                    "summary": summary,
                    "engine": "llm",
                    "model": meta.get("model", ""),
                }
            )
        if len(out) == len(items):
            return out
        return None
    except llm.LLMError as exc:
        log.warning("资讯 LLM 解读失败，回退规则引擎：%s", exc)
        return None


# ------------------------------------------------------------------ 组装入口

async def get_stock_news(
    code: str, name: str = "", days: int = NEWS_DAYS, limit: int = NEWS_LIMIT, force: bool = False
) -> dict[str, Any]:
    """返回 {\"items\": [...], \"meta\": {...}}。

    - 原始资讯从数据源抓取，缓存 RAW_TTL；
    - 解读结果单独缓存 INTERP_TTL（资讯未变时复用，不重复打 LLM）；
    - force=True 时强制刷新（前端「刷新」按钮）。
    """
    code = code.strip()
    market = resolve_market(code)

    raw_key = f"news:raw:{code}"
    interp_key = f"news:interp:{code}:{days}"

    async def load_raw() -> tuple[list[dict[str, Any]], str]:
        # 始终抓取 NEWS_DAYS（30 天）全量进缓存，days 过滤在缓存外用内存做（与研报一致）
        items, src = await registry().news_src(code, market, name, NEWS_DAYS, limit)
        return (
            [
                {
                    "id": it.id,
                    "date": it.date,
                    "source": it.source,
                    "title": it.title,
                    "summary": it.summary,
                    "url": it.url,
                }
                for it in items
            ],
            src,
        )

    try:
        all_items, src_name = await cache.get_or_set(raw_key, RAW_TTL, load_raw, force=force)
    except ProviderError as exc:
        return {"items": [], "meta": {"error": f"资讯获取失败：{exc}", "engine": "none", "total": 0, "source": ""}}

    # 按时间范围过滤（days 为展示窗口；<=0 或 >=NEWS_DAYS 时不过滤）
    since = (now() - timedelta(days=days)).strftime("%Y-%m-%d") if 0 < days < NEWS_DAYS else ""
    ranged = [it for it in all_items if not since or (it.get("date", "") or "")[:10] >= since]
    items = ranged[:limit]

    if not items:
        return {"items": [], "meta": {"error": "该时间范围内暂无相关资讯", "engine": "none", "total": 0, "source": src_name, "days": days}}

    async def load_interp() -> list[dict[str, Any]] | None:
        got = await _llm_interpret(items, code, name)
        if got:
            return got
        # LLM 不可用/失败：规则引擎逐条兜底
        return [rule_interpret(item) for item in items]

    interp: list[dict[str, Any]] | None = None
    try:
        interp = await cache.get_or_set(interp_key, INTERP_TTL, load_interp, force=force)
    except Exception as exc:  # noqa: BLE001
        log.warning("资讯解读失败：%s", exc)
        interp = None

    engine = "rule"
    if interp and interp[0].get("engine") == "llm":
        engine = "llm"

    merged = []
    for i, item in enumerate(items):
        row = {**item}
        row["interpretation"] = interp[i] if interp and i < len(interp) else rule_interpret(item)
        merged.append(row)

    return {
        "items": merged,
        "meta": {
            "code": code,
            "name": name,
            "days": days,
            "total": len(merged),
            "engine": engine,
            "source": src_name,
            "fetched_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
