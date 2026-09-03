"""Reports。"""
from __future__ import annotations

from tests._common import *  # noqa: F401,F403  公共导入见 tests/_common.py
from backend.indicators import build_ma, summarize_flow, support_resistance
from backend.providers import registry
from backend.providers.base import Bar, FlowDay

def test_reports_interpret() -> None:
    # 规则解读：评级本身即信号 + 标题关键词修正
    buy = reports.rule_interpret({"rating": "买入", "title": "业绩预增，目标价上调", "source": "国海证券"})
    over = reports.rule_interpret({"rating": "增持", "title": "盈利增速抬升", "source": "平安证券"})
    flat = reports.rule_interpret({"rating": "中性", "title": "经营平稳", "source": "华泰证券"})
    sell = reports.rule_interpret({"rating": "减持", "title": "业绩下滑风险", "source": "某券商"})
    assert (buy["sentiment"] == "利好" and buy["engine"] == "rule"), str(buy)
    assert (over["sentiment"] == "利好"), str(over)
    assert (flat["sentiment"] == "中性"), str(flat)
    assert (sell["sentiment"] == "利空"), str(sell)
    # 评级中性但标题强利好词 -> 利好（关键词修正）
    mixed = reports.rule_interpret({"rating": "中性", "title": "业绩超预期大增", "source": "某券商"})
    assert (mixed["sentiment"] == "利好"), str(mixed)

    # 近一年评级分布统计：只计入 since 之后的条目
    dist = reports.rating_distribution(
        [
            {"date": "2026-08-01", "rating": "买入"},
            {"date": "2026-07-01", "rating": "增持"},
            {"date": "2026-06-01", "rating": "增持"},
            {"date": "2025-06-01", "rating": "买入"},   # 一年前，应排除
            {"date": "2026-05-01", "rating": ""},        # 无评级 -> --
        ],
        "2025-08-17",
    )
    assert (dist == {"买入": 1, "增持": 2, "--": 1}), str(dist)


