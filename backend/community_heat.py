"""社区讨论热度：抓取各股票社区的热度信号，聚合为板块讨论热度排名。

数据源（均为公开接口真实数据，非模拟；curl 实测口径见各源注释）：
- 东方财富·股吧人气榜（实测可用）：
  POST https://emappdata.eastmoney.com/stockrank/getAllCurrentList
  body {"appId":"appId01","globalId":"786e4c21-70dc-435a-93bb-38",
        "marketType":"","pageNo":1,"pageSize":100}
  → data[{sc:"SH600127", rk:1, rc:0, hisRc:69}]
  rk = 当前股吧人气排名（浏览/发帖/讨论量口径）。rc/hisRc 语义上游未说明，
  不确定不编造，本模块只使用 rk。
- 同花顺·热帖：无公开接口，【数据缺失】暂未接入（结构预留）。
- 雪球·热股：接口需登录 token（匿名 cookie 引导拿不到 xq_a_token），
  【数据缺失】暂未接入（结构预留）。

聚合口径：人气榜前 GUBA_POOL_SIZE 只 → push2delay ulist 批量补
（f12 代码/f14 名称/f2 价格/f3 涨跌幅/f100 所属行业板块，实测支持）→
板块热度 = Σ(POOL+1-rank)：排名越靠前讨论热度贡献越大 → 板块榜按热度
降序，每板块给讨论热度归一分、入榜股数、平均涨幅与代表股（热度贡献 top3）。

结果缓存 30 分钟（社区热度变化慢，抓取频率不用太高）；失败结果短缓存
避免故障期间反复打外部接口。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .cache import cache
from .providers.base import client
from .utils import TZ, now

log = logging.getLogger("hotspot.community")

TTL = 1800.0            # 正常缓存 30 分钟：抓取频率不用太高
FAIL_TTL = 120.0        # 失败短缓存：全部源故障期间不反复打外部接口
GUBA_POOL_SIZE = 100    # 股吧人气榜取前 N 名
ULIST_BATCH = 50        # push2delay ulist 单批 secids 数（保守值）
BOARD_LIMIT = 15        # 板块榜最多返回条数
REPRESENT_TOP = 3       # 每板块代表股数量

# sc 前缀 → 东财 secid 市场号（ulist 用）。北交所等未实测映射，宁缺毋滥直接跳过。
_SC_MARKET = {"SH": "1.", "SZ": "0."}

# 数据源登记表：status ok=已接入 / missing=【数据缺失】（前端如实标注）
SOURCES: list[dict[str, str]] = [
    {"name": "东方财富·股吧人气榜", "status": "ok",
     "note": "股吧浏览/发帖讨论量排名，取前 100 名聚合"},
    {"name": "同花顺·热帖", "status": "missing",
     "note": "【数据缺失】无公开接口，暂未接入"},
    {"name": "雪球·热股", "status": "missing",
     "note": "【数据缺失】接口需登录 token，暂未接入"},
]


def _parse_rank_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """解析股吧人气榜响应 → [{code, secid, rank}]（纯函数）。

    sc 形如 "SH600127"/"SZ002579"；北交所等无映射前缀的跳过并计数。
    """
    data = (payload or {}).get("data") or []
    rows: list[dict[str, Any]] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        sc = str(it.get("sc") or "")
        rank = it.get("rk")
        if len(sc) < 3 or not isinstance(rank, int):
            continue
        market = _SC_MARKET.get(sc[:2].upper())
        if market is None:
            continue
        rows.append({"code": sc[2:], "secid": market + sc[2:], "rank": rank})
    return rows


def _parse_ulist_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """解析 ulist 行情响应 → {code: {name, price, chg_pct, board}}（纯函数）。

    f2 价格 / f3 涨跌幅 / f100 所属行业板块；上游对无数据可能返回 "-"，
    统一清洗为 None，绝不编造。
    """
    out: dict[str, dict[str, Any]] = {}
    diff = ((payload or {}).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):  # 极端情况下上游返回 {code: {...}} 结构
        diff = list(diff.values())
    for it in diff:
        if not isinstance(it, dict):
            continue
        code = str(it.get("f12") or "")
        if not code:
            continue

        def _num(v: Any) -> float | None:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if f == f else None  # 过滤 NaN

        out[code] = {
            "name": it.get("f14") or None,
            "price": _num(it.get("f2")),
            "chg_pct": _num(it.get("f3")),
            # 上游对无数据返回字符串 "-"，必须清洗为 None，否则 "-" 板块会参与聚合
            "board": (it.get("f100") or None) if it.get("f100") not in ("-", "") else None,
        }
    return out


def _aggregate(
    ranks: list[dict[str, Any]], meta: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """人气榜排名 + 个股元数据 → 板块讨论热度榜（纯函数）。

    权重：POOL+1-rank（榜首贡献 POOL，榜尾贡献 1）。板块热度 = 成员权重和；
    heat_norm = 100×heat/max；代表股 = 板块内权重最高的前 REPRESENT_TOP 只。
    """
    pool = len(ranks)
    boards: dict[str, dict[str, Any]] = {}
    matched = 0
    for r in ranks:
        m = meta.get(r["code"])
        if not m or not m.get("board"):
            continue
        matched += 1
        weight = pool + 1 - r["rank"]
        b = boards.setdefault(m["board"], {"heat": 0.0, "stocks": []})
        b["heat"] += weight
        b["stocks"].append({
            "code": r["code"], "name": m.get("name"), "rank": r["rank"],
            "weight": weight, "chg_pct": m.get("chg_pct"),
        })
    if not boards:
        return []
    max_heat = max(b["heat"] for b in boards.values()) or 1.0
    items: list[dict[str, Any]] = []
    for name, b in boards.items():
        stocks = sorted(b["stocks"], key=lambda s: s["weight"], reverse=True)
        chgs = [s["chg_pct"] for s in stocks if s["chg_pct"] is not None]
        items.append({
            "name": name,
            "heat": round(b["heat"], 1),
            "heat_norm": round(100.0 * b["heat"] / max_heat, 1),
            "stock_count": len(stocks),
            "avg_chg": round(sum(chgs) / len(chgs), 2) if chgs else None,
            "stocks": stocks[:REPRESENT_TOP],
        })
    items.sort(key=lambda x: x["heat"], reverse=True)
    return items[:BOARD_LIMIT]


async def _fetch_guba_rank(limit: int = GUBA_POOL_SIZE) -> list[dict[str, Any]] | None:
    """拉取股吧人气榜前 N 名。失败返回 None（不抛，由上层降级）。"""
    body = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
            "marketType": "", "pageNo": 1, "pageSize": limit}
    try:
        resp = await asyncio.wait_for(
            client().post(
                "https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
                headers={"Content-Type": "application/json",
                         "Referer": "https://guba.eastmoney.com/"},
                json=body,
            ), timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or payload.get("status") not in (0, "0"):
            log.warning("股吧人气榜返回异常状态: %s", str(payload)[:200])
            return None
        rows = _parse_rank_rows(payload)
        return rows or None
    except Exception as exc:  # noqa: BLE001 - 单源失败降级，不让异常冒泡
        log.warning("股吧人气榜拉取失败: %s", exc)
        return None


async def _fetch_stock_meta(
    secids: list[str],
) -> dict[str, dict[str, Any]]:
    """批量拉取个股元数据（名称/价格/涨跌幅/所属板块）。分批并发，失败批静默跳过。"""
    result: dict[str, dict[str, Any]] = {}
    batches = [secids[i:i + ULIST_BATCH] for i in range(0, len(secids), ULIST_BATCH)]

    async def _one(batch: list[str]) -> None:
        url = ("https://push2delay.eastmoney.com/api/qt/ulist.np/get"
               "?fltt=2&np=1&fields=f12,f14,f2,f3,f100&secids=" + ",".join(batch))
        # 单批失败重试一次：整批 50 只股票静默丢失会让板块聚合严重失真
        # （实测一次瞬时失败即丢掉约一半映射），重试代价低收益高。
        for attempt in range(2):
            try:
                resp = await asyncio.wait_for(
                    client().get(url, headers={"Referer": "https://quote.eastmoney.com/"}),
                    timeout=10.0)
                resp.raise_for_status()
                result.update(_parse_ulist_rows(resp.json()))
                return
            except Exception as exc:  # noqa: BLE001 - 重试一次后仍失败则跳过该批
                if attempt == 0:
                    await asyncio.sleep(0.6)
                else:
                    log.warning("个股元数据批次拉取失败(%d 只): %s", len(batch), exc)

    await asyncio.gather(*[_one(b) for b in batches])
    return result


async def _load() -> dict[str, Any]:
    ranks = await _fetch_guba_rank()
    if not ranks:
        raise RuntimeError("股吧人气榜不可用")
    meta = await _fetch_stock_meta([r["secid"] for r in ranks])
    items = _aggregate(ranks, meta)
    return {
        "items": items,
        "meta": {
            "generated_at": now().strftime("%Y-%m-%d %H:%M:%S"),
            "pool_size": len(ranks),
            "matched": sum(len(i["stocks"]) for i in items),
            "ttl_minutes": int(TTL // 60),
            "sources": SOURCES,
        },
    }


async def get_community_heat(force: bool = False) -> dict[str, Any]:
    """社区讨论热度榜（板块聚合）。失败降级为空榜单 + meta.error，绝不编造。"""
    key = "hotspot:community"
    try:
        return await cache.get_or_set(key, TTL, _load, force=force)
    except Exception as exc:  # noqa: BLE001
        log.debug("社区讨论热度降级: %s", exc)
        result: dict[str, Any] = {
            "items": [],
            "meta": {
                "error": f"社区讨论热度获取失败：{exc}",
                "generated_at": now().strftime("%Y-%m-%d %H:%M:%S"),
                "pool_size": 0,
                "matched": 0,
                "ttl_minutes": int(TTL // 60),
                "sources": SOURCES,
            },
        }
        # 失败结果也短暂缓存：源故障期间反复请求不再重打外部接口
        cache.put(key, result, FAIL_TTL)
        return result
