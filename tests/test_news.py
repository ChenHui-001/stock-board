"""News。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from backend import (
    analysis, api, cache, check_sources, hotspot, hotspot_ai, hotspot_search,
    indicators, llm, llmcfg, metrics, news, providers, reports, scorecfg,
    service, storage, value_screener, valuecfg,
)
from backend.config import settings
from backend.indicators import build_ma, summarize_flow, support_resistance
from backend.providers import registry
from backend.providers.base import Bar, FlowDay

def test_news_interpret() -> None:
    # 规则解读关键词情绪
    bull = news.rule_interpret({"title": "公司中标大订单", "summary": "业绩预增 50%"})
    bear = news.rule_interpret({"title": "股东减持", "summary": "收到处罚决定书"})
    flat = news.rule_interpret({"title": "召开股东大会", "summary": "审议日常议案"})
    assert (bull["sentiment"] == "利好" and bull["engine"] == "rule"), str(bull)
    assert (bear["sentiment"] == "利空"), str(bear)
    assert (flat["sentiment"] == "中性"), str(flat)

    # 资讯评分进规则引擎：利好加分、封顶 ±12、无资讯不引用
    detail = {
        "quote": {"code": "600000", "name": "浦发银行", "price": 9.0, "prev_close": 9.1, "change_pct": -1.1},
        "boards": [], "kline": [], "ma": [],
        "ma_summary": {"arrangement": "交织", "above_count": 0, "above": [], "below": [], "series": {}},
        "support_resistance": {}, "fund_flow": {"rows": [], "summary": {}},
        "margin": {"rows": [], "summary": {}},
        "status": {"tags": [], "trend": {}},
    }
    news_items = [
        {"title": "中标大单", "summary": "s", "date": "2026-08-17 10:00:00",
         "interpretation": {"sentiment": "利好", "impact": "高", "summary": "正面"}},
        {"title": "获准收购", "summary": "s", "date": "2026-08-16 10:00:00",
         "interpretation": {"sentiment": "利好", "impact": "中", "summary": "正面"}},
        {"title": "被罚款", "summary": "s", "date": "2026-08-15 10:00:00",
         "interpretation": {"sentiment": "利空", "impact": "中", "summary": "负面"}},
    ]
    fb = analysis.rule_based(detail, news_items)
    assert ("资讯面 2 利好/1 利空（计 +4 分）" in fb["advice"]["reason"]), fb["advice"]["reason"]
    assert (any("资讯面偏暖" in o for o in fb["risk"]["opportunities"]))
    fb0 = analysis.rule_based(detail, None)
    assert ("资讯面" not in fb0["advice"]["reason"])
    payload = analysis.build_payload(detail, news_items)
    assert (len(payload.get("市场资讯_近30日") or []) == 3)

    # 券商研报面进规则引擎：利好加分、封顶 ±15、无研报不引用
    report_items = [
        {"rating": "买入", "title": "业绩预增", "source": "国海证券", "date": "2026-08-10 09:00:00",
         "interpretation": {"sentiment": "利好", "impact": "高", "summary": "正面"}},
        {"rating": "增持", "title": "盈利增速抬升", "source": "平安证券", "date": "2026-08-05 09:00:00",
         "interpretation": {"sentiment": "利好", "impact": "中", "summary": "正面"}},
        {"rating": "减持", "title": "业绩下滑", "source": "某券商", "date": "2026-08-01 09:00:00",
         "interpretation": {"sentiment": "利空", "impact": "中", "summary": "负面"}},
    ]
    fb_r = analysis.rule_based(detail, None, report_items)
    assert ("研报面 2 利好/1 利空（计 +5 分）" in fb_r["advice"]["reason"]), fb_r["advice"]["reason"]
    assert (any("券商研报面偏暖" in o for o in fb_r["risk"]["opportunities"]))
    fb_no_r = analysis.rule_based(detail, None, None)
    assert ("研报面" not in fb_no_r["advice"]["reason"])
    payload_r = analysis.build_payload(detail, None, report_items)
    assert (len(payload_r.get("券商观点_近30日") or []) == 3)


