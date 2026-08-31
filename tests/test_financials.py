"""Financials。"""
from __future__ import annotations


def _mk_detail(**kw):
    """构造最小可用的 detail dict，供规则引擎 / payload 测试复用。"""
    d = {
        'quote': {'code': '600000', 'name': '浦发银行', 'price': 9.0, 'prev_close': 9.1, 'change_pct': -1.1},
        'boards': [], 'kline': [], 'ma': [],
        'ma_summary': {'arrangement': '交织', 'above_count': 0, 'above': [], 'below': [], 'series': {}},
        'support_resistance': {}, 'fund_flow': {'rows': [], 'summary': {}},
        'margin': {'rows': [], 'summary': {}},
        'status': {'tags': [], 'trend': {}},
    }
    d.update(kw)
    return d


def _ma_item(w, v, slope='上行'):
    return {'window': w, 'value': v, 'slope': slope, 'position': '站上',
            'deviation_pct': 0.0, 'slope_pct': 0.0}

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

def test_financials() -> None:
    from backend.providers.webparse import parse_financial_html, parse_news_html, parse_report_html

    ths_json = (
        '<p id="main">{"title":["科目\\\\时间",["营业总收入","元",0,false,true],'
        '["营业总收入同比增长率","",0,false,true]],'
        '"report":[["2026-06-30","2026-03-31"],["12.5亿","10亿"],["25%","20%"]]}</p>'
    )
    embedded_rows = parse_financial_html(ths_json, "ths")
    assert (embedded_rows[0].revenue == 1.25e9 and embedded_rows[0].revenue_yoy == 25), str(embedded_rows)
    linkage = (
        f'<div id="linkagedata">[{{"seq":1,"ctime":{int(time.time())},'
        '"curl":"http://news.10jqka.com.cn/field/test.shtml",'
        '"title":"公司签订重大订单","source":"测试媒体"}]</div>'
    )
    linkage_rows = parse_news_html(linkage, "ths", 30, 5)
    assert (linkage_rows[0].title == "公司签订重大订单" and linkage_rows[0].source == "测试媒体"), str(linkage_rows)
    eastmoney_json = (
        '<script>var initdata = {"data":[{"title":"业绩增长点评",'
        '"orgSName":"国金证券","publishDate":"2026-08-18 00:00:00.000",'
        '"infoCode":"APTEST001","sRatingName":"增持",'
        '"researcher":"张三"}]};</script>'
    )
    embedded_reports = parse_report_html(eastmoney_json, "eastmoney", 5)
    assert (embedded_reports[0].rating == "增持" and embedded_reports[0].source == "国金证券"), str(embedded_reports)

    detail = _mk_detail(financials={"source": "ths", "rows": [
        {"period": "2026H1", "date": "2026-06-30", "revenue": 1.25e9, "revenue_yoy": 25,
         "net_profit": 2.5e8, "net_profit_yoy": 25, "roe": 11.2, "debt_ratio": 40},
    ]})
    fb = analysis.rule_based(detail)
    scores = fb["advice"]["scores"]
    assert (scores["fundamental"] > 0 and "2026H1" in fb["fundamental"]["period"]), str(fb["fundamental"])
    assert (any("基本面偏强" in str(x) for x in fb["risk"]["opportunities"])), str(fb["risk"]["opportunities"])
    reversed_detail = _mk_detail(financials={"rows": [
        {"period": "2025FY", "date": "2025-12-31", "revenue_yoy": -20, "net_profit_yoy": -20},
        {"period": "2026H1", "date": "2026-06-30", "revenue_yoy": 10, "net_profit_yoy": 10},
    ]})
    reversed_fb = analysis.rule_based(reversed_detail)
    assert (reversed_fb["fundamental"]["period"] == "2026H1"), str(reversed_fb["fundamental"])
    payload = analysis.build_payload(detail)
    fin_payload = payload.get("财报数据_季报中报") or {}
    assert (fin_payload.get("最新报告期") == "2026H1"), str(fin_payload)
    stale_detail = _mk_detail(financials={"source": "ths", "stale": True, "error": "网页暂不可用", "rows": [
        {"period": "2026H1", "date": "2026-06-30", "revenue_yoy": 10, "net_profit_yoy": 10},
    ]})
    stale_payload = analysis.build_payload(stale_detail)["财报数据_季报中报"]
    stale_fb = analysis.rule_based(stale_detail)
    assert ("使用上次成功缓存" in stale_payload.get("数据状态", "")), str(stale_payload)
    assert ("缓存数据" in stale_fb["fundamental"]["summary"]), str(stale_fb["fundamental"])


