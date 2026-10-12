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

from tests._common import *  # noqa: F401,F403  公共导入见 tests/_common.py
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




def test_webparse_edges() -> None:
    """webparse 边界分支：负数/单位换算、无效行过滤、过期新闻、缺 infoCode 降级。"""
    import backend.providers.webparse as wp

    # ---- _number：括号负数 / 亿 / 万 / 无效占位 ----
    assert wp._number("(3.5万)") == -35000.0
    assert wp._number("1.2亿") == 1.2e8
    assert wp._number("1,234.5") == 1234.5
    assert wp._number("--") is None and wp._number("暂无") is None and wp._number(None) is None

    # ---- parse_financial_html：无 main JSON → ValueError；烂 JSON → ValueError ----
    for bad in ("<html>没有财报</html>", '<p id="main">not-json</p>',
                '<p id="main">{"title":[],"report":[]}</p>'):
        try:
            wp.parse_financial_html(bad, "ths")
            raise AssertionError(f"应抛 ValueError: {bad[:30]}")
        except ValueError:
            pass

    # 无效科目行被跳过；全 None 的日期列不产出；日期倒序
    ths = (
        '<p id="main">{"title":["科目",["乱写的科目"],["营业总收入"]],'
        '"report":[["2026-06-30","2026-03-31"],["x-y-z"],["5亿","4亿"]]}</p>'
    )
    rows = wp.parse_financial_html(ths, "ths")
    assert [r.date for r in rows] == ["2026-06-30", "2026-03-31"], str(rows)
    assert rows[0].revenue == 5e8 and rows[1].revenue == 4e8

    # ---- parse_news_html：过期/短标题/无链接/坏 ctime 过滤，日期倒序 + limit ----
    now_ts = int(time.time())
    mk = lambda seq, t, title, curl: f'{{"seq":{seq},"ctime":{t},"curl":"{curl}","title":"{title}","source":"测"}}'
    news_html = (
        '<div id="linkagedata">['
        + mk(1, now_ts, "新公告标题甲", "http://x/1.shtml") + ","
        + mk(2, now_ts - 86400 * 40, "四十字前的旧闻标题", "http://x/2.shtml") + ","   # 超 30 天
        + mk(3, now_ts, "短", "http://x/3.shtml") + ","                               # 标题<4
        + mk(4, now_ts, "没有链接的标题乙", "") + ","                                  # 无 curl
        + mk(5, 0, "零时间戳的旧标题", "http://x/5.shtml") + ","                        # ctime=0 → 1970 被过期过滤
        + mk(6, now_ts - 60, "六十秒前的标题丙", "http://x/6.shtml") + ","
        + mk(7, now_ts - 120, "一百二秒前标题丁", "http://x/7.shtml")
        + "]</div>"
    )
    items = wp.parse_news_html(news_html, "ths", days=30, limit=5)
    assert [i.id for i in items] == ["1", "6", "7"], str(items)   # 旧的/短的/无链接/坏时间戳全过滤

    # ---- parse_report_html：无 infoCode → url 空 + id=title；坏日期行跳过；orgName 兜底 ----
    em = (
        '<script>var initdata = {"data":['
        '{"title":"无码研报标题","orgName":"某券商研究所","publishDate":"2026-08-01 00:00:00.000"},'
        '{"title":"坏日期研报","publishDate":"not-a-date"},'
        '{"title":"带码研报","orgSName":"头部券商","publishDate":"2026/08/05","infoCode":"APX1","sRatingName":"买入"}'
        "]};</script>"
    )
    reports = wp.parse_report_html(em, "eastmoney", 5)
    assert [r.id for r in reports] == ["APX1", "无码研报标题"], str(reports)   # 有码 id=infoCode；坏日期行被滤；斜杠日期已归一化
    assert reports[0].url.endswith("infocode=APX1") and reports[0].source == "头部券商" and reports[0].date == "2026-08-05"
    no_code = reports[1]
    assert no_code.url == "" and no_code.id == "无码研报标题" and no_code.source == "某券商研究所"
