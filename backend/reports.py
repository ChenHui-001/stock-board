"""券商研报：从同花顺个股页内嵌数据抓取，按日期倒序展示。

数据源：同花顺 basic.10jqka.com.cn/{code}/news.html 内嵌研报 JSON
（与同花顺个股页「新闻公告」展示一致，web 层数据）。
结果整体缓存（1 小时），避免反复抓取同花顺页面。
"""
from __future__ import annotations

import logging
from typing import Any

from .cache import cache
from .providers import ProviderError, registry
from .utils import now, resolve_market

log = logging.getLogger("reports")

REPORT_LIMIT = 20
REPORT_TTL = 3600.0


async def get_reports(code: str, limit: int = REPORT_LIMIT, force: bool = False) -> dict[str, Any]:
    """返回 {\"items\": [...], \"meta\": {...}}，按日期倒序。"""
    code = code.strip()
    market = resolve_market(code)
    key = f"reports:{code}.{market}"

    async def load() -> tuple[list[dict[str, Any]], str]:
        items, src = await registry().reports(code, market, limit)
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
        items, src_name = await cache.get_or_set(key, REPORT_TTL, load, force=force)
    except ProviderError as exc:
        return {"items": [], "meta": {"error": f"研报获取失败：{exc}", "source": "", "total": 0}}

    return {
        "items": items or [],
        "meta": {
            "code": code,
            "total": len(items or []),
            "source": src_name,
            "fetched_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
