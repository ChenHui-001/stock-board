"""券商研报：从同花顺个股页内嵌数据抓取，逐条附 AI 解读。

数据源：同花顺 basic.10jqka.com.cn/{code}/news.html 内嵌研报 JSON
（与同花顺个股页「新闻公告」展示一致，web 层数据）。

解读走两条路径（与资讯解读一致）：
- LLM 可用：一次批量调用对全部研报输出情绪/影响/解读（结构化 JSON）。
- LLM 不可用或失败：规则引擎兜底——评级本身即信号（买入=利好、增持=偏利好、
  减持/卖出=利空），叠加标题关键词，输出 sentiment/impact/summary。

结果整体进内存缓存（原始研报 1 小时 + 解读 30 分钟），避免反复抓取页面与打 LLM。
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

log = logging.getLogger("reports")

REPORT_LIMIT = 20
REPORT_TTL = 3600.0
INTERP_TTL = 1800.0

# ---------------------------------------------------------------- 评级分布

def rating_distribution(items: list[dict[str, Any]], since: str) -> dict[str, int]:
    """统计 since（YYYY-MM-DD，含）以来的研报评级分布。"""
    dist: dict[str, int] = {}
    for it in items:
        if it.get("date", "") < since:
            continue
        r = it.get("rating") or "--"
        dist[r] = dist.get(r, 0) + 1
    return dist


# ---------------------------------------------------------------- 规则解读
# 评级本身即信号（同花顺研报评级取值）
_RATING_SENTIMENT = {
    "买入": "利好",
    "强烈推荐": "利好",
    "推荐": "利好",
    "增持": "利好",
    "超配": "利好",
    "中性": "中性",
    "持有": "中性",
    "观望": "中性",
    "减持": "利空",
    "卖出": "利空",
    "回避": "利空",
}
_BULL_WORDS = [
    "业绩预增", "净利润增长", "超预期", "上调", "目标价", "盈利增速", "扭亏",
    "增长", "改善", "景气", "红利", "推荐", "买入", "增持", "龙头", "市占率提升",
    "成本优化", "利润改善", "两位数增长", "创新高", "跑赢", "优于",
]
_BEAR_WORDS = [
    "下滑", "下降", "低于预期", "下调", "承压", "风险", "减值", "亏损", "放缓",
    "压力", "不确定性", "竞争加剧", "增速回落", "分化", "谨慎", "回避",
]
_HIGH_IMPACT = [
    "大幅", "显著", "重磅", "重大", "首次", "深度", "强烈", "超预期",
]


def _match_count(text: str, words: list[str]) -> int:
    return sum(1 for w in words if w in text)


def rule_interpret(item: dict[str, Any]) -> dict[str, Any]:
    """规则解读单条研报（LLM 不可用/失败时的兜底）。"""
    rating = str(item.get("rating") or "")
    text = f"{item.get('title', '')} {item.get('source', '')} {rating}"
    bull, bear = _match_count(text, _BULL_WORDS), _match_count(text, _BEAR_WORDS)

    # 评级信号优先，标题关键词做修正
    base = _RATING_SENTIMENT.get(rating, "中性")
    if bull > bear and bull > 0:
        sentiment = "利好"
    elif bear > bull and bear > 0:
        sentiment = "利空"
    else:
        sentiment = base

    if _match_count(text, _HIGH_IMPACT) > 0:
        impact = "高"
    elif sentiment == "中性":
        impact = "低"
    else:
        impact = "中"

    org = item.get("source") or "该券商"
    words = [w for w in _BULL_WORDS + _BEAR_WORDS if w in text][:3]
    if sentiment == "利好":
        summary = (
            f"{org}给出「{rating or '看好'}」评级，报告提及（{'、'.join(words) or '积极因素'}），"
            "整体偏乐观，可作为持股/关注的参考信号。"
        )
    elif sentiment == "利空":
        summary = (
            f"{org}给出「{rating or '谨慎'}」评级，报告提及（{'、'.join(words) or '风险因素'}），"
            "整体偏谨慎，注意基本面走弱风险。"
        )
    else:
        summary = f"{org}给出「{rating or '中性'}」评级，观点中性，参考价值一般，建议结合财报与行情验证。"
    return {"sentiment": sentiment, "impact": impact, "summary": summary, "engine": "rule"}


# ---------------------------------------------------------------- LLM 解读
_INTERP_SYSTEM = """你是一名 A 股券商研报解读助手。你会收到某只股票的 N 条券商研报（JSON 数组），每条含机构、研究员、评级、标题。

请对每一条研报给出解读，用于辅助该股的持仓决策。要求：
1. 只输出一个 JSON 对象，结构为 {"items": [{"idx": 0, "sentiment": "利好", "impact": "中", "summary": "..."}]}，不要输出任何其他文字或代码块。
2. sentiment 只取三选一：利好 / 利空 / 中性。
3. impact 只取三选一：高 / 中 / 低。
4. summary 为 60 字以内的一句话解读，概括研报核心观点，说明对股价的参考意义。
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
            "机构": item.get("source", ""),
            "研究员": item.get("researcher", ""),
            "评级": item.get("rating", ""),
            "标题": item.get("title", ""),
        }
        for i, item in enumerate(items)
    ]
    user = (
        f"以下是 {name}({code}) 的 {len(payload)} 条券商研报，请逐条解读：\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=1)}"
    )
    try:
        raw, meta = await llm.chat_json(_INTERP_SYSTEM, user)
        rows = raw.get("items") or []
        if not isinstance(rows, list):
            return None
        by_idx: dict[int, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row.get("idx", -1))
            except (TypeError, ValueError):
                continue
            by_idx[idx] = row
        out: list[dict[str, Any]] = []
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
        log.warning("研报 LLM 解读失败，回退规则引擎：%s", exc)
        return None


# ---------------------------------------------------------------- 组装入口

async def get_reports(
    code: str,
    name: str = "",
    days: int = 365,
    limit: int = REPORT_LIMIT,
    force: bool = False,
) -> dict[str, Any]:
    """返回 {\"items\": [...], \"meta\": {...}}，按日期倒序，逐条附 interpretation。

    days 为时间范围（30/90/365/0=全部）：原始研报全量进缓存（1 小时），
    每次按范围过滤展示条数与评级分布；解读缓存按 (code, days) 分开。
    """
    code = code.strip()
    market = resolve_market(code)

    raw_key = f"reports:raw:{code}"
    interp_key = f"reports:interp:{code}:{days}"

    async def load_raw() -> tuple[list[dict[str, Any]], str]:
        # 拉全量（上限 200 条防滥用），范围过滤在缓存外用内存做
        items, src = await registry().reports(code, market, 200)
        return (
            [
                {
                    "id": it.id,
                    "date": it.date,
                    "source": it.source,
                    "researcher": it.researcher,
                    "rating": it.rating,
                    "title": it.title,
                    "url": it.url,
                }
                for it in items
            ],
            src,
        )

    try:
        all_items, src_name = await cache.get_or_set(raw_key, REPORT_TTL, load_raw, force=force)
    except ProviderError as exc:
        return {"items": [], "meta": {"error": f"研报获取失败：{exc}", "engine": "none", "total": 0, "source": ""}}

    # 按时间范围过滤（days<=0 表示全部）
    since = (now() - timedelta(days=days)).strftime("%Y-%m-%d") if days and days > 0 else ""
    ranged = [it for it in all_items if not since or it.get("date", "") >= since]
    items = ranged[:limit]
    rating_dist = rating_distribution(ranged, since)

    if not items:
        return {"items": [], "meta": {"error": "该时间范围内暂无券商研报", "engine": "none", "total": 0, "source": src_name, "days": days}}

    async def load_interp() -> list[dict[str, Any]] | None:
        got = await _llm_interpret(items, code, name)
        if got:
            return got
        return [rule_interpret(item) for item in items]

    interp: list[dict[str, Any]] | None = None
    try:
        interp = await cache.get_or_set(interp_key, INTERP_TTL, load_interp, force=force)
    except Exception as exc:  # noqa: BLE001
        log.warning("研报解读失败：%s", exc)
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
        "rating_dist": rating_dist,
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
