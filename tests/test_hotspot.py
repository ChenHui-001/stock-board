"""Hotspot。"""
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

def test_hotspot() -> None:
    from backend import hotspot
    import json as _json

    now_ts = int(time.time())
    old_ts = now_ts - 3600  # 一小时前，应在窗口外

    # 同花顺：ctime 为 unix 秒
    ths_payload = _json.dumps({"code": "200", "data": {"list": [
        {"id": "1", "title": "富时中国A50指数期货跌2%", "digest": "摘要A",
         "url": "http://news.10jqka.com.cn/1.shtml", "ctime": now_ts, "source": ""},
        {"id": "2", "title": "旧闻不展示", "digest": "摘要B",
         "url": "http://news.10jqka.com.cn/2.shtml", "ctime": old_ts, "source": "测试媒体"},
        {"id": "bad", "title": "时间缺失", "digest": "x", "url": "", "ctime": None},
    ]}})
    ths_rows = hotspot._parse_ths(ths_payload)
    assert (len(ths_rows) == 2 and ths_rows[0]["source"] == "同花顺"), str(ths_rows)

    # 东方财富：showTime 为字符串
    em_payload = _json.dumps({"code": "1", "data": {"list": [
        {"code": "E1", "title": "财联社：央行开展逆回购操作", "summary": "摘要C",
         "showTime": "2026-08-19 10:30:00", "mediaName": "财联社", "url": "http://finance.eastmoney.com/a/E1.html"},
        {"code": "E2", "title": "彭博：美股期货上涨", "summary": "摘要D",
         "showTime": "2026-08-19 10:31:00", "mediaName": "彭博", "url": "http://finance.eastmoney.com/a/E2.html"},
        {"code": "E3", "title": "坏时间", "summary": "x",
         "showTime": "not-a-date", "mediaName": "东方财富", "url": ""},
    ]}})
    em_rows = hotspot._parse_em(em_payload)
    assert (len(em_rows) == 2 and em_rows[0]["source"] == "财联社"), str(em_rows)

    # 新浪：rich_text 拆标题/摘要
    sina_payload = _json.dumps({"result": {"data": {"feed": {"list": [
        {"id": "S1", "create_time": "2026-08-19 10:32:00",
         "rich_text": "【澎湃新闻】沪深两市成交额突破万亿", "docurl": "http://finance.sina.com.cn/S1.html"},
        {"id": "S2", "create_time": "2026-08-19 10:33:00",
         "rich_text": "无括号纯文本内容", "docurl": ""},
    ]}}}})
    sina_rows = hotspot._parse_sina(sina_payload)
    assert (sina_rows[0]["title"] == "澎湃新闻" and sina_rows[0]["summary"] == "沪深两市成交额突破万亿"), str(sina_rows[0])
    assert (sina_rows[1]["title"] == "无括号纯文本内容"), str(sina_rows[1])

    # 华尔街见闻：display_time 为 unix 秒，content_text 无【】包裹，首句拆标题
    wscn_payload = _json.dumps({"code": 20000, "message": "OK", "data": {"items": [
        {"id": 3152710, "title": "", "content_text": "浙江：7月份，规模以上工业增加值同比增长7.4%。",
         "display_time": now_ts, "uri": "https://wallstreetcn.com/livenews/3152710"},
        {"id": 3152711, "title": "", "content_text": "沪深两市成交额突破万亿。A股放量上行，北向资金净流入。",
         "display_time": now_ts - 10, "uri": ""},
        {"id": 3152712, "title": "", "content_text": "无时间条目", "display_time": None, "uri": ""},
    ]}})
    wscn_rows = hotspot._parse_wscn(wscn_payload)
    assert (len(wscn_rows) == 2), str(wscn_rows)
    assert (wscn_rows[0]["title"] == "浙江：7月份，规模以上工业增加值同比增长7.4%。"
          and wscn_rows[0]["summary"] == ""), str(wscn_rows[0])
    assert (wscn_rows[1]["title"] == "沪深两市成交额突破万亿。"
          and wscn_rows[1]["summary"] == "A股放量上行，北向资金净流入。"), str(wscn_rows[1])
    assert (wscn_rows[0]["source"] == "华尔街见闻" and wscn_rows[0]["origin"] == "华尔街见闻"), str(wscn_rows[0])

    # 窗口过滤：旧条目剔除
    in_rows = [r for r in ths_rows if hotspot._in_window(r["ts"], 30)]
    assert (len(in_rows) == 1 and in_rows[0]["id"] == "1"), str(in_rows)
    assert (hotspot._in_window(None, 30) is False)

    # 跨源去重：同一标题只留一条（取最新时间），其余保留；华尔街见闻也参与
    dup = [
        {"id": "t1", "title": "央行开展逆回购操作", "ts": now_ts - 100, "origin": "同花顺", "source": "同花顺"},
        {"id": "e1", "title": "央行开展逆回购操作！", "ts": now_ts - 50, "origin": "东方财富", "source": "财联社"},
        {"id": "w1", "title": "央行开展逆回购操作，", "ts": now_ts - 20, "origin": "华尔街见闻", "source": "华尔街见闻"},
        {"id": "s1", "title": "富时中国A50指数期货跌2%", "ts": now_ts - 10, "origin": "新浪财经", "source": "新浪财经"},
    ]
    merged = hotspot._merge(dup)
    assert (len(merged) == 2 and any(m["id"] == "w1" for m in merged)
          and not any(m["id"] in ("t1", "e1") for m in merged)), str(merged)
    assert (merged[0]["id"] == "s1" and merged[1]["id"] == "w1"), str(merged)
    assert (hotspot._title_fp("央行开展逆回购操作！") == hotspot._title_fp("央行开展逆回购操作")), hotspot._title_fp("央行开展逆回购操作！")
    assert (hotspot._title_fp("【央行开展逆回购操作】") == hotspot._title_fp("央行开展逆回购操作")), hotspot._title_fp("【央行开展逆回购操作】")

    # 重点媒体标注（金十数据只是快讯数据源，不在用户点名的重点媒体白名单内，
    # 因此不在 _HOT_MEDIA 中——这里只断言名单内媒体的命中/不命中）。
    assert (hotspot.is_hot_media("财联社") is True)

    # _FEEDS 注册项：两源已加入且解析函数正确绑定（用 id 断言避免闭包到原对象上）
    from backend import hotspot as _hp
    feed_names = [name for name, _u, _h, _p, _t in _hp._FEEDS]
    feed_tiers = {name: tier for name, _u, _h, _p, tier in _hp._FEEDS}
    feed_parsers = {name: parse for name, _u, _h, parse, _t in _hp._FEEDS}
    assert (len(feed_names) == 6), str(feed_names)
    assert ("财联社" in feed_names and "金十数据" in feed_names), str(feed_names)
    assert (feed_parsers.get("财联社") is _hp._parse_cls), str(feed_parsers)
    assert (feed_parsers.get("金十数据") is _hp._parse_jin10), str(feed_parsers)
    # 超时分级：快源应 ≤ 4s，标准源 ≤ 6s，金十应走 slow (≥ fast)。验证差异确实存在。
    tier_set = set(feed_tiers.values())
    assert ("fast" in tier_set and "normal" in tier_set and "slow" in tier_set), str(feed_tiers)
    assert (feed_tiers.get("金十数据") == "slow"), str(feed_tiers)
    assert (feed_tiers.get("同花顺") == "fast" and feed_tiers.get("新浪财经") == "fast"
          and feed_tiers.get("华尔街见闻") == "fast"), str(feed_tiers)
    # 整体预算应小于所有源超时之和，避免一个慢源把响应拖到 34s
    all_timeouts = sum(_hp._TIMEOUT_BY_TIER.get(t, 6.0) for t in feed_tiers.values())
    assert (settings.HOTSPOT_BUDGET < all_timeouts), f"budget={settings.HOTSPOT_BUDGET}, sum={all_timeouts}"
    # 整体预算收紧到 8s：实测 6 源全正常 < 0.5s，8s 已留足余量。
    assert (settings.HOTSPOT_BUDGET == 8.0), f"budget={settings.HOTSPOT_BUDGET}"
    # SourceStat：连续失败 → 熔断 → 冷却 → 自动恢复，半开放重试
    from backend.hotspot import SourceStat
    import asyncio as _aio

    async def _run_circuit() -> bool:
        """熔断生命周期：调用 SourceStat 的 async API（加锁后改 async）。"""
        stat = SourceStat("测试源", open_at=2, cooldown=0.5)
        assert (await stat.is_open() is False), ""
        await stat.record_failure()
        assert (await stat.is_open() is False
              and stat.consecutive_failures == 1), ""
        await stat.record_failure()
        assert (await stat.is_open() is True
              and stat.consecutive_failures == 2), ""
        await _aio.sleep(0.6)
        assert (await stat.is_open() is False), ""
        await stat.record_success()
        assert (stat.consecutive_failures == 0), ""
        return True

    _aio.run(_run_circuit())
    # _fetch_one 重试参数化为指数序列
    import inspect
    sig = inspect.signature(_hp._fetch_one)
    retry_param = sig.parameters.get("retry_backoffs")
    assert (retry_param is not None), ""
    if retry_param is not None:
        assert (retry_param.default == (1.0, 2.0)), str(retry_param.default)
    # 熔断配置项
    assert (settings.HOTSPOT_CIRCUIT_OPEN_AT == 3), str(settings.HOTSPOT_CIRCUIT_OPEN_AT)
    assert (settings.HOTSPOT_CIRCUIT_COOLDOWN == 120.0), str(settings.HOTSPOT_CIRCUIT_COOLDOWN)

    # Prometheus 指标：模块可导入、5 个指标注册、记录后能取回值
    from backend import metrics as _mt
    assert (hasattr(_mt, "SOURCE_REQUESTS")), ""
    assert (all(hasattr(_mt, n) for n in (
              "SOURCE_REQUESTS", "SOURCE_FAILURES", "SOURCE_CIRCUIT_OPEN",
              "SOURCE_ITEMS", "SOURCE_DURATION",
          ))), ""

    async def _run_metrics() -> bool:
        """驱动一次 SourceStat + 直方图打点，验证 Gauge 增量与 Counter 递增。"""
        stat = SourceStat("指标测试源", open_at=2, cooldown=60.0)
        await stat.record_failure()           # 失败 1 → gauge=1, circuit=0
        await stat.record_failure()           # 失败 2 → 触发熔断，gauge=2, circuit=1
        # 取 Gauge 当前值（无需 scrape）
        from prometheus_client import REGISTRY
        def _gauge(name: str) -> float:
            return REGISTRY.get_sample_value(
                name, {"source": "指标测试源"}) or 0.0
        assert (_gauge("hotspot_source_consecutive_failures") == 2.0), str(_gauge("hotspot_source_consecutive_failures"))
        assert (_gauge("hotspot_source_circuit_open") == 1.0), str(_gauge("hotspot_source_circuit_open"))
        await stat.record_success()           # 恢复 → gauge=0, circuit=0
        assert (_gauge("hotspot_source_consecutive_failures") == 0.0), str(_gauge("hotspot_source_consecutive_failures"))
        assert (_gauge("hotspot_source_circuit_open") == 0.0), str(_gauge("hotspot_source_circuit_open"))
        # 模拟一次抓取：record_request + observe_duration
        _mt.SOURCE_REQUESTS.labels(source="指标测试源", result="success").inc()
        _mt.observe_duration("指标测试源", "success", 0.123)
        assert (REGISTRY.get_sample_value(
                  "hotspot_source_requests_total",
                  {"source": "指标测试源", "result": "success"}) or 0 >= 1), ""
        # export() 输出非空 + content_type 正确
        body, ctype = _mt.export()
        assert (b"hotspot_source_requests_total" in body), f"len={len(body)}"
        assert ("text/plain" in ctype), ctype
        return True

    _aio.run(_run_metrics())

    # 财联社签名与 URL：参数固定后签名可复现，URL 含 sign 字段
    sign1 = _hp._cls_sign(_hp._CLS_ROLL_PARAMS)
    sign2 = _hp._cls_sign({"os": "web", "sv": "7.7.5", "app": "CailianpressWeb", "rn": "50", "last_time": ""})
    assert (isinstance(sign1, str) and len(sign1) == 32 and sign1 == sign2), sign1
    url = _hp._cls_url()
    assert ("sign=" in url and "rn=50" in url), url

    # 财联社解析：含电头 → 剥除后首句作标题；纯文本 → 首句兜底；时间缺失 → 剔除
    cls_payload = _json.dumps({"errno": 0, "data": {"roll_data": [
        {"id": 1001, "ctime": now_ts, "content": "财联社8月24日电，央行开展逆回购操作。规模为500亿元，期限7天。",
         "brief": "", "shareurl": "https://api3.cls.cn/share/article/1001?os=web"},
        {"id": 1002, "ctime": now_ts - 30, "content": "光通信板块盘前普跌，Coherent跌超5%，Lumentum跌近5%。",
         "brief": "", "shareurl": ""},
        {"id": 1003, "ctime": None, "content": "财联社8月24日电，无效时间。", "brief": "", "shareurl": ""},
        {"id": 1004, "ctime": now_ts - 60, "content": "", "brief": "", "shareurl": ""},
    ]}})
    cls_rows = _hp._parse_cls(cls_payload)
    assert (len(cls_rows) == 2 and cls_rows[0]["title"] == "央行开展逆回购操作。"
          and cls_rows[0]["source"] == "财联社"), str(cls_rows)
    assert (cls_rows[1]["title"] == "光通信板块盘前普跌，Coherent跌超5%，Lumentum跌近5%。"), str(cls_rows[1])
    assert (all(r["id"] not in ("1003", "1004") for r in cls_rows)), str(cls_rows)

    # 财联社容错：非 JSON / data 缺失 / roll_data 非列表
    assert (_hp._parse_cls("") == []), "expected []"
    assert (_hp._parse_cls('{"errno":0}') == []), "expected []"
    assert (_hp._parse_cls('{"data":{"roll_data":"x"}}') == []), "expected []"

    # 金十解析：【标题】摘要 → 拆标题并剥电头；裸文本 → 首句兜底；time 非 datetime → 剔除
    jin10_payload = _json.dumps({"status": 200, "message": "OK", "data": [
        {"id": "J1", "data": {"content": "【福瑞医科：上半年净利润6394万】金十数据8月24日讯，同比增长23.12%。"}, "time": "2026-08-24 16:05:39"},
        {"id": "J2", "data": {"content": "伦敦金属交易所（LME）铜注册仓单增加5.14万吨，为5月以来最大增幅。"}, "time": "2026-08-24 16:05:30"},
        {"id": "J3", "data": {"content": "金十数据8月24日讯，无效时间。"}, "time": "not-a-date"},
        {"id": "J4", "data": {"content": ""}, "time": "2026-08-24 16:05:30"},
    ]})
    jin10_rows = _hp._parse_jin10(jin10_payload)
    assert (len(jin10_rows) == 2 and jin10_rows[0]["title"] == "福瑞医科：上半年净利润6394万"
          and jin10_rows[0]["source"] == "金十数据"), str(jin10_rows)
    assert (jin10_rows[1]["title"] == "伦敦金属交易所（LME）铜注册仓单增加5.14万吨，为5月以来最大增幅。"), str(jin10_rows[1])
    assert (all(r["id"] not in ("J3", "J4") for r in jin10_rows)), str(jin10_rows)

    # 金十容错：顶层 data 缺失 / 非 JSON
    assert (_hp._parse_jin10('{"status":200}') == []), "expected []"
    assert (_hp._parse_jin10("oops") == []), "expected []"

    assert (hotspot.is_hot_media("彭博社") is True)
    assert (hotspot.is_hot_media("某地方日报") is False)
    assert (hotspot.is_hot_media("") is False)

    # get_hotspot 兜底：全部源失败时返回结构化错误而非抛异常；
    # 且失败结果短暂缓存，故障期间反复请求不重打外部快讯接口
    from backend.cache import cache as _cache
    calls: dict[str, int] = {"n": 0}

    async def _boom(minutes: int) -> dict:
        calls["n"] += 1
        raise hotspot.ProviderError("全部热点快讯源均不可用")

    async def _fail() -> dict:
        orig = hotspot._load
        hotspot._load = _boom
        try:
            first = await hotspot.get_hotspot(41)
            second = await hotspot.get_hotspot(41)
            return first, second
        finally:
            hotspot._load = orig
            _cache.drop("hotspot:41")

    f1, f2 = asyncio.run(_fail())
    assert (f1["items"] == [] and "error" in f1["meta"]), str(f1)
    assert (calls["n"] == 1 and f2["meta"]["error"] == f1["meta"]["error"]), f"loads={calls['n']}"





def test_hotspot_ai() -> None:
    from backend import hotspot_ai
    from backend.cache import cache as _cache
    from backend.providers.base import SearchItem

    # 规则路径：情绪 + 行业识别 + 检索关键词
    sent, bull, bear, watch, kws = hotspot_ai.rule_analyze(
        "光伏行业迎来政策利好，储能需求爆发", "多家组件厂商订单饱满"
    )
    assert (sent == "利好"), sent
    assert (any(x["industry"] == "光伏" for x in bull) and any(x["industry"] == "储能" for x in bull)), str(bull)
    assert ("光伏" in kws and "储能" in kws), str(kws)

    sent2, _b, bear2, _w, kws2 = hotspot_ai.rule_analyze(
        "煤炭价格大跌，煤企利润承压", "多家煤企下调全年产量目标"
    )
    assert (sent2 == "利空"), sent2
    assert (any(x["industry"] == "煤炭" for x in bear2)), str(bear2)

    sent3, _b3, _be3, watch3, _k3 = hotspot_ai.rule_analyze("光伏行业召开行业大会", "会议讨论行业规范")
    assert (sent3 == "中性" and any(x["industry"] == "光伏" for x in watch3)), str(watch3)

    # 关键词提取：去重 + 限长 6
    kws4 = hotspot_ai._extract_keywords(
        "光伏 光伏 储能 半导体 芯片 医药 白酒 券商 军工 算力 机器人 黄金 煤炭 石油 有色"
    )
    assert (len(kws4) == 6 and kws4.count("光伏") == 1), str(kws4)

    # 股票代码过滤：普通 A 股保留，ETF/LOF/基金剔除（东财 suggest 会把 ETF 标成 A股）
    assert (hotspot_ai._is_stock_code("601012", "SH")
          and hotspot_ai._is_stock_code("688981", "SH"))
    assert (hotspot_ai._is_stock_code("000001", "SZ")
          and hotspot_ai._is_stock_code("300750", "SZ"))
    assert (hotspot_ai._is_stock_code("920002", "BJ"))
    assert (not hotspot_ai._is_stock_code("515070", "SH")
          and not hotspot_ai._is_stock_code("510300", "SH"))
    assert (not hotspot_ai._is_stock_code("159819", "SZ")
          and not hotspot_ai._is_stock_code("161631", "SZ"))

    # 关联股票解析 + analyze_news 缓存（mock 真实搜索接口，不触网）
    async def _run() -> None:
        orig_search, orig_quotes, orig_avail = (
            hotspot_ai._search_one, hotspot_ai._with_quotes, llm.available,
        )
        llm.available = lambda: False
        try:
            async def fake_search(kw: str) -> tuple:
                if kw == "光伏":
                    return ([SearchItem(code="601012", market="SH", name="隆基绿能"),
                             SearchItem(code="600438", market="SH", name="通威股份")], "东方财富")
                if kw == "储能":
                    return ([SearchItem(code="300274", market="SZ", name="阳光电源"),
                             SearchItem(code="601012", market="SH", name="隆基绿能")], "同花顺")
                return [], ""
            async def _noop(s):
                return s
            hotspot_ai._search_one = fake_search
            hotspot_ai._with_quotes = _noop

            stocks = await hotspot_ai._resolve_stocks(["光伏", "储能"])
            assert (len(stocks) == 3), str(stocks)
            assert (stocks[0]["code"] == "601012"), str(stocks)
            # 命中明细：每个检索词 + 检索来源
            top_matches = stocks[0].get("matches") or []
            assert (len(top_matches) == 2
                  and {m["keyword"] for m in top_matches} == {"光伏", "储能"}), str(top_matches)
            assert ({m["source"] for m in top_matches} == {"东方财富", "同花顺"}), str(top_matches)
            assert ((stocks[1].get("matches") or [{}])[0].get("source") == "东方财富"), str(stocks[1].get("matches"))

            calls = {"n": 0}
            async def fake_search2(kw: str) -> tuple:
                calls["n"] += 1
                return [SearchItem(code="601012", market="SH", name="隆基绿能")], "东方财富"
            hotspot_ai._search_one = fake_search2
            r1 = await hotspot_ai.analyze_news("光伏政策利好", "")
            r2 = await hotspot_ai.analyze_news("光伏政策利好", "")
            assert (r1["ok"] and r1["engine"] == "rule"), str(r1)[:120]
            assert (calls["n"] == 1), f"n={calls['n']}"
            assert (r1["stocks"] and r1["stocks"][0]["code"] == "601012"), str(r1["stocks"])
        finally:
            hotspot_ai._search_one = orig_search
            hotspot_ai._with_quotes = orig_quotes
            llm.available = orig_avail
            _cache.drop_prefix("hotspot_ai:")

    asyncio.run(_run())

    # _with_quotes 行情补齐（mock service.get_quotes，防漏 for 推导回归）
    from backend.providers.base import Quote as _Q
    async def _quotes_test() -> None:
        orig_get_quotes = service.get_quotes
        async def fake_get_quotes(keys, force=False):
            return {"601012.SH": _Q(code="601012", market="SH", name="隆基绿能",
                                     price=18.5, change_pct=2.3, board="光伏")}
        service.get_quotes = fake_get_quotes
        try:
            got = await hotspot_ai._with_quotes([
                {"code": "601012", "market": "SH", "name": "隆基绿能", "keywords": ["光伏"]},
            ])
            assert (got[0]["price"] == 18.5
                  and got[0]["change_pct"] == 2.3 and got[0]["board"] == "光伏"), str(got)
            assert (got[0]["reason"] == "光伏"), str(got)
        finally:
            service.get_quotes = orig_get_quotes
    asyncio.run(_quotes_test())

    empty = asyncio.run(hotspot_ai.analyze_news("  "))
    assert (empty["ok"] is False), str(empty)


