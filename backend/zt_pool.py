"""东财涨停池共享取数：首页自选股涨停标记 + 价值投资候选池共用。

为什么单独抽模块：service.watchlist_board（首页）与 value_screener（候选池）
都要同一份涨停池数据，共享 TTL 缓存后首页多刷几次也不会多打接口。

字段语义（push2ex getTopicZTPool 行字段）：
- ``lbc``  连板数：连续几天收盘涨停（1 = 首板/今天第一个板）
- ``zttj`` 涨停统计 ``{"days": N, "ct": M}`` 即「N天M板」——最近 N 个交易日里
  M 次涨停（允许中间断板），days==ct 时等价于 N 连板
- ``fund`` 封单资金、``fbt`` 首次封板时间、``hybk`` 行业板块

数据原则：失败返回空结构（count=0），由调用方按【数据缺失】降级，绝不编造。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from . import cache as cache_mod
from .providers.base import fetch

log = logging.getLogger("zt_pool")

_cache = cache_mod.cache
_CACHE_KEY = "zt_pool:shared"
_CACHE_TTL = 60.0  # 盘中 1 分钟足够；封单/连板数变化频率低于行情

_MARKET_PREFIXES = ("6", "9", "5")  # 沪市：60x/68x/9xx/5xx 基金；其余按深市


def zt_label(lianban: int | None, days: int | None, ct: int | None) -> str:
    """把连板数 + 涨停统计转成「首板 / N连板 / X天Y板」标签。

    规则（与游资口径一致）：
    - 连板数 >= 2 → 「N连板」
    - 连板数 == 1 且 days == ct → 「首板」（今天唯一一个板，无历史板）
    - 连板数 == 1 且 days > ct → 「X天Y板」（隔日断板再涨停）
    """
    if not lianban or lianban < 1:
        return ""
    if lianban >= 2:
        return f"{lianban}连板"
    if days and ct and days > ct:
        return f"{days}天{ct}板"
    return "首板"


def match_zt(pool: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """从涨停池结构构建 code → 涨停信息 映射（供自选股逐行标注）。"""
    out: dict[str, dict[str, Any]] = {}
    for row in (pool or {}).get("rows") or []:
        code = str(row.get("code") or "")
        if len(code) != 6:
            continue
        lianban = row.get("lianban")
        days = row.get("zttj_days")
        ct = row.get("zttj_ct")
        out[code] = {
            "lianban": lianban,
            "zttj_days": days,
            "zttj_ct": ct,
            "board": row.get("board") or "",
            "seal_amount": row.get("seal_amount"),
            "label": zt_label(lianban, days, ct),
        }
    return out


async def get_zt_pool(force: bool = False) -> dict[str, Any]:
    """东财涨停池（含连板/几天几板/封单），TTL 缓存共享。失败返回空结构。"""

    async def _load() -> dict[str, Any]:
        return await _fetch_zt_pool()

    return await _cache.get_or_set(_CACHE_KEY, _CACHE_TTL, _load, force=force)


async def _fetch_zt_pool() -> dict[str, Any]:
    try:
        resp = await fetch(
            "https://push2ex.eastmoney.com/getTopicZTPool",
            params={
                "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                "Pageindex": 0, "pagesize": 400, "sort": "fbt:asc",
                "date": datetime.now().strftime("%Y%m%d"),
            },
            headers={"Referer": "https://quote.eastmoney.com/ztb/"},
        )
        data = (resp.json() or {}).get("data") or {}
        rows: list[dict[str, Any]] = []
        for r in data.get("pool") or []:
            code = str(r.get("c") or "")
            if len(code) != 6:
                continue
            market = "SH" if code.startswith(_MARKET_PREFIXES) else "SZ"
            zttj = r.get("zttj") or {}
            lianban = r.get("lbc")
            days = zttj.get("days")
            ct = zttj.get("ct")
            rows.append({
                "code": code, "market": market,
                "name": r.get("n") or "",
                "change_pct": round((r.get("zdp") or 0) / 100, 2),
                "turnover": r.get("hs"),
                "volume_ratio": r.get("lb"),
                "lianban": lianban,                 # 连板数
                "zttj_days": days,                  # 几天
                "zttj_ct": ct,                      # 几板
                "seal_amount": r.get("fund"),       # 封单额
                "board": r.get("hybk") or "",       # 所属板块
                "label": zt_label(lianban, days, ct),
            })
        return {"count": data.get("tc") or len(rows), "rows": rows}
    except Exception as exc:  # noqa: BLE001
        log.warning("涨停池获取失败：%s", exc)
        return {"count": 0, "rows": []}


def annotate(codes: Iterable[str], pool: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """便捷封装：给定 6 位代码集合，返回命中的涨停信息映射。"""
    zt_map = match_zt(pool)
    return {c: zt_map[c] for c in codes if c in zt_map}
