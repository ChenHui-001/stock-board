"""社区讨论热度模块（community_heat）：解析/聚合纯函数 + 降级路径。"""
from __future__ import annotations

import json as _json

from tests._common import *  # noqa: F401,F403  公共导入见 tests/_common.py


def test_parse_rank_rows() -> None:
    from backend import community_heat as ch

    payload = _json.loads(_json.dumps({
        "status": 0, "data": [
            {"sc": "SH600127", "rk": 1, "rc": 0, "hisRc": 0},
            {"sc": "SZ002579", "rk": 2, "rc": 0, "hisRc": 2},
            {"sc": "BJ832566", "rk": 3, "rc": 0},   # 北交所无映射 → 跳过
            {"sc": "", "rk": 4},                     # 空 sc → 跳过
            {"sc": "SH601086"},                      # 缺 rk → 跳过
            "bad-row",                               # 非法行 → 跳过
        ],
    }))
    rows = ch._parse_rank_rows(payload)
    assert [r["code"] for r in rows] == ["600127", "002579"], str(rows)
    assert rows[0]["secid"] == "1.600127" and rows[0]["rank"] == 1, str(rows)
    assert rows[1]["secid"] == "0.002579" and rows[1]["rank"] == 2, str(rows)
    # 空响应不炸
    assert ch._parse_rank_rows({}) == []
    assert ch._parse_rank_rows(None) == []


def test_parse_ulist_rows() -> None:
    from backend import community_heat as ch

    payload = _json.loads(_json.dumps({
        "rc": 0, "data": {"total": 3, "diff": [
            {"f2": 14.71, "f3": 8.72, "f12": "600127", "f14": "金健米业", "f100": "农产品加工"},
            {"f2": "-", "f3": "-", "f12": "002579", "f14": "中京电子", "f100": "元件"},  # 无数据为 "-"
            {"f2": None, "f3": None, "f12": "605577", "f14": "", "f100": "-"},          # 全缺 → None
            {"f12": ""},                                                                 # 空 code → 跳过
        ]},
    }))
    meta = ch._parse_ulist_rows(payload)
    assert set(meta.keys()) == {"600127", "002579", "605577"}, str(meta.keys())
    assert meta["600127"]["board"] == "农产品加工" and meta["600127"]["chg_pct"] == 8.72
    assert meta["002579"]["price"] is None and meta["002579"]["chg_pct"] is None
    assert meta["605577"]["board"] is None and meta["605577"]["name"] is None
    # diff 为 dict 结构的容错
    alt = {"data": {"diff": {"a": {"f12": "600001", "f14": "X", "f2": 1, "f3": 2, "f100": "B"}}}}
    assert ch._parse_ulist_rows(alt)["600001"]["board"] == "B"


def test_aggregate() -> None:
    from backend import community_heat as ch

    ranks = [
        {"code": "600127", "secid": "1.600127", "rank": 1},
        {"code": "002579", "secid": "0.002579", "rank": 2},
        {"code": "605577", "secid": "1.605577", "rank": 3},
        {"code": "600000", "secid": "1.600000", "rank": 4},   # 元数据缺失 → 跳过
        {"code": "600001", "secid": "1.600001", "rank": 5},
    ]
    meta = {
        "600127": {"name": "金健米业", "price": 14.71, "chg_pct": 8.72, "board": "农产品加工"},
        "002579": {"name": "中京电子", "price": 17.06, "chg_pct": 9.99, "board": "元件"},
        "605577": {"name": "龙版传媒", "price": 18.76, "chg_pct": 9.64, "board": "出版"},
        "600001": {"name": " 甲乙丙 ", "price": 1.0, "chg_pct": None, "board": "元件"},
    }
    pool = len(ranks)  # 5
    items = ch._aggregate(ranks, meta)
    by_name = {i["name"]: i for i in items}
    # 权重 = pool+1-rank：rank1→5、rank2→4、rank5→1
    assert by_name["农产品加工"]["heat"] == 5.0, str(items)
    assert by_name["元件"]["heat"] == 4.0 + 1.0, str(items)
    # 排序按 heat 降序；并列时保持插入顺序（sorted 稳定，农产品加工先入 dict）
    assert items[0]["name"] == "农产品加工", str([i["name"] for i in items])
    # heat_norm 归一：max=5 → 两个板块都为 100
    assert by_name["元件"]["heat_norm"] == 100.0
    assert by_name["农产品加工"]["heat_norm"] == 100.0
    # 代表股 top3 按权重降序，权重低的 600000（元数据缺失）不入榜
    assert [s["code"] for s in by_name["元件"]["stocks"]] == ["002579", "600001"], str(by_name["元件"])
    assert by_name["元件"]["stock_count"] == 2
    # avg_chg 只统计有涨跌幅的成员：元件成员 002579(9.99) + 600001(None 跳过)
    assert by_name["元件"]["avg_chg"] == 9.99, str(by_name["元件"])
    # 空输入
    assert ch._aggregate([], {}) == []
    assert ch._aggregate(ranks, {}) == []


def test_load_and_degrade(monkeypatch) -> None:
    from backend import community_heat as ch

    ranks = [
        {"code": "600127", "secid": "1.600127", "rank": 1},
        {"code": "002579", "secid": "0.002579", "rank": 2},
    ]
    meta = {
        "600127": {"name": "金健米业", "price": 14.71, "chg_pct": 8.72, "board": "农产品加工"},
        "002579": {"name": "中京电子", "price": 17.06, "chg_pct": 9.99, "board": "元件"},
    }
    monkeypatch.setattr(ch, "_fetch_guba_rank", lambda limit=ch.GUBA_POOL_SIZE: _ok(ranks))
    monkeypatch.setattr(ch, "_fetch_stock_meta", lambda secids: _ok(meta))

    async def _run() -> None:
        result = await ch._load()
        assert result["items"], str(result)
        assert result["meta"]["pool_size"] == 2
        assert result["meta"]["ttl_minutes"] == 30
        # sources 登记表齐全且如实标注缺失
        src = {s["name"]: s["status"] for s in result["meta"]["sources"]}
        assert src["东方财富·股吧人气榜"] == "ok", str(src)
        assert src.get("同花顺·热帖") == "missing" and src.get("雪球·热股") == "missing"

        # 降级：榜不可用 → items=[] + meta.error，不抛异常
        monkeypatch.setattr(ch, "_fetch_guba_rank", lambda limit=ch.GUBA_POOL_SIZE: _ok(None))
        bad = await ch.get_community_heat(force=True)
        assert bad["items"] == [] and "error" in bad["meta"], str(bad)

    asyncio.run(_run())


class _ok:
    """把同步值包成 awaitable（模拟 async fetcher 返回）。"""

    def __init__(self, value):
        self._value = value

    def __await__(self):
        if False:
            yield  # pragma: no cover
        return self._value
