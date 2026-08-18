"""离线冒烟测试：CI 门禁用，不依赖外部网络 / 数据源 / 浏览器。

覆盖：模块可导入、LLM JSON 修复阶梯、LLM 配置指纹、缓存单飞与过期重载、
AI 每股票单飞锁、均线与支撑压力指标、数据源注册表装配。
运行：python backend/smoke_test.py（退出码 0=通过）
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# 用临时数据目录，避免污染工作区 / CI 环境
_tmp = tempfile.mkdtemp(prefix="board-smoke-")
os.environ["DATA_DIR"] = _tmp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import analysis, api, cache, llm, llmcfg, news, reports, service, storage  # noqa: E402
from backend.indicators import build_ma, summarize_flow, support_resistance  # noqa: E402
from backend.providers import registry  # noqa: E402
from backend.providers.base import Bar  # noqa: E402

FAILED: list[str] = []
TOTAL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"[{'ok' if cond else 'FAIL'}] {name}" + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ------------------------------------------------------------------ LLM JSON 修复阶梯
def test_json_repair() -> None:
    cases: dict[str, str] = {
        '{"action": "持有观望", "confidence": 80}': "持有观望",
        '```json\n{"action": "积极持仓/加仓"}\n```': "积极持仓/加仓",
        '```JSON\n{"action": "清仓离场"}\n```': "清仓离场",
        "前文寒暄 {\"action\": \"减仓规避\"} 结尾废话": "减仓规避",
        '{"action": "清仓离场", "list": [1, 2,]}': "清仓离场",
        '{"action": "持有观望", "trend": {"summary": "多头", "short": "截断': "持有观望",
        '{"advice": {"action": "持有观望", "reason": "依据", "support": 12.34': "持有观望",
        '{"action": "减仓规避", "a": {"b": [1,2,3], "c": {"d": 1}': "减仓规避",
        '{"action": "持有观望", "x": 1}{"action": "减仓规避"}': "持有观望",
    }
    for raw, want in cases.items():
        try:
            obj = llm._extract_json(raw)
            got = obj.get("action") or (obj.get("advice") or {}).get("action")
            check(f"JSON修复: {raw[:24]}...", got == want, f"got={got!r}")
        except llm.LLMError as exc:
            check(f"JSON修复: {raw[:24]}...", False, str(exc))
    try:
        llm._extract_json("这不是 JSON 内容")
        check("JSON修复: 垃圾输入应报错", False)
    except llm.LLMError:
        check("JSON修复: 垃圾输入应报错", True)


# ------------------------------------------------------------------ LLM 配置指纹
def test_fingerprint() -> None:
    storage.init_db()
    f1 = llmcfg.fingerprint()
    storage.set_llm_config({"enabled": True, "base_url": "https://x/v1", "model": "m1", "api_key": "k", "json_mode": True})
    f2 = llmcfg.fingerprint()
    storage.set_llm_config({"enabled": True, "base_url": "https://x/v1", "model": "m2", "api_key": "k", "json_mode": True})
    f3 = llmcfg.fingerprint()
    check("指纹随配置变化", f1 != f2 and f2 != f3)
    storage.clear_llm_config()# ------------------------------------------------------------------ 模型列表过滤
# 云端 /models 里混有 embedding/图片/语音等非对话模型，应被过滤、对话模型应保留
def test_model_filter() -> None:
    keep = ["deepseek-chat", "deepseek-reasoner", "qwen-plus", "glm-4-plus",
            "moonshot-v1-32k", "gpt-4o", "llama3.1:70b", "kimi-k2-thinking"]
    drop = ["text-embedding-3-large", "bge-m3", "dall-e-3", "whisper-1",
            "sdxl-turbo", "rerank-3", "image-1", "flux-dev"]
    bad_keep = [m for m in keep if llm._NON_CHAT_RE.search(m)]
    bad_drop = [m for m in drop if not llm._NON_CHAT_RE.search(m)]
    check("模型过滤（保留对话/剔除非对话）", not bad_keep and not bad_drop,
          f"keep误杀={bad_keep} drop漏网={bad_drop}")


# ------------------------------------------------------------------ 资讯解读
def test_news_interpret() -> None:
    # 规则解读关键词情绪
    bull = news.rule_interpret({"title": "公司中标大订单", "summary": "业绩预增 50%"})
    bear = news.rule_interpret({"title": "股东减持", "summary": "收到处罚决定书"})
    flat = news.rule_interpret({"title": "召开股东大会", "summary": "审议日常议案"})
    check("资讯规则解读: 利好词", bull["sentiment"] == "利好" and bull["engine"] == "rule", str(bull))
    check("资讯规则解读: 利空词", bear["sentiment"] == "利空", str(bear))
    check("资讯规则解读: 中性", flat["sentiment"] == "中性", str(flat))

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
    check("资讯评分写入建议依据", "资讯面 2 利好/1 利空（计 +4 分）" in fb["advice"]["reason"], fb["advice"]["reason"])
    check("资讯情绪进机会面", any("资讯面偏暖" in o for o in fb["risk"]["opportunities"]))
    fb0 = analysis.rule_based(detail, None)
    check("无资讯不引用", "资讯面" not in fb0["advice"]["reason"])
    payload = analysis.build_payload(detail, news_items)
    check("投喂数据含资讯段", len(payload.get("市场资讯_近30日") or []) == 3)

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
    check("研报评分写入建议依据", "研报面 2 利好/1 利空（计 +5 分）" in fb_r["advice"]["reason"], fb_r["advice"]["reason"])
    check("研报情绪进机会面", any("券商研报面偏暖" in o for o in fb_r["risk"]["opportunities"]))
    fb_no_r = analysis.rule_based(detail, None, None)
    check("无研报不引用", "研报面" not in fb_no_r["advice"]["reason"])
    payload_r = analysis.build_payload(detail, None, report_items)
    check("投喂数据含券商观点段", len(payload_r.get("券商观点_近30日") or []) == 3)


# ------------------------------------------------------------------ 研报解读
def test_reports_interpret() -> None:
    # 规则解读：评级本身即信号 + 标题关键词修正
    buy = reports.rule_interpret({"rating": "买入", "title": "业绩预增，目标价上调", "source": "国海证券"})
    over = reports.rule_interpret({"rating": "增持", "title": "盈利增速抬升", "source": "平安证券"})
    flat = reports.rule_interpret({"rating": "中性", "title": "经营平稳", "source": "华泰证券"})
    sell = reports.rule_interpret({"rating": "减持", "title": "业绩下滑风险", "source": "某券商"})
    check("研报规则解读: 买入=利好", buy["sentiment"] == "利好" and buy["engine"] == "rule", str(buy))
    check("研报规则解读: 增持=利好", over["sentiment"] == "利好", str(over))
    check("研报规则解读: 中性", flat["sentiment"] == "中性", str(flat))
    check("研报规则解读: 减持=利空", sell["sentiment"] == "利空", str(sell))
    # 评级中性但标题强利好词 -> 利好（关键词修正）
    mixed = reports.rule_interpret({"rating": "中性", "title": "业绩超预期大增", "source": "某券商"})
    check("研报规则解读: 关键词修正评级", mixed["sentiment"] == "利好", str(mixed))

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
    check("研报评级分布统计", dist == {"买入": 1, "增持": 2, "--": 1}, str(dist))


# ------------------------------------------------------------------ 规则引擎精准化
def _mk_detail(**kw) -> dict:
    d = {
        "quote": {"code": "600000", "name": "浦发银行", "price": 9.0, "prev_close": 9.1, "change_pct": -1.1},
        "boards": [], "kline": [], "ma": [],
        "ma_summary": {"arrangement": "交织", "above_count": 0, "above": [], "below": [], "series": {}},
        "support_resistance": {}, "fund_flow": {"rows": [], "summary": {}},
        "margin": {"rows": [], "summary": {}},
        "status": {"tags": [], "trend": {}},
    }
    d.update(kw)
    return d


def _ma_item(w: int, v: float, slope: str = "上行") -> dict:
    return {"window": w, "value": v, "slope": slope, "position": "站上", "deviation_pct": 0.0}


def test_financials() -> None:
    from backend.providers.webparse import parse_financial_html, parse_news_html, parse_report_html

    ths_json = (
        '<p id="main">{"title":["科目\\\\时间",["营业总收入","元",0,false,true],'
        '["营业总收入同比增长率","",0,false,true]],'
        '"report":[["2026-06-30","2026-03-31"],["12.5亿","10亿"],["25%","20%"]]}</p>'
    )
    embedded_rows = parse_financial_html(ths_json, "ths")
    check("同花顺财报内嵌JSON", embedded_rows[0].revenue == 1.25e9 and embedded_rows[0].revenue_yoy == 25, str(embedded_rows))
    linkage = (
        f'<div id="linkagedata">[{{"seq":1,"ctime":{int(time.time())},'
        '"curl":"http://news.10jqka.com.cn/field/test.shtml",'
        '"title":"公司签订重大订单","source":"测试媒体"}]</div>'
    )
    linkage_rows = parse_news_html(linkage, "ths", 30, 5)
    check("同花顺资讯内嵌JSON", linkage_rows[0].title == "公司签订重大订单" and linkage_rows[0].source == "测试媒体", str(linkage_rows))
    eastmoney_json = (
        '<script>var initdata = {"data":[{"title":"业绩增长点评",'
        '"orgSName":"国金证券","publishDate":"2026-08-18 00:00:00.000",'
        '"infoCode":"APTEST001","sRatingName":"增持",'
        '"researcher":"张三"}]};</script>'
    )
    embedded_reports = parse_report_html(eastmoney_json, "eastmoney", 5)
    check("东财研报内嵌JSON", embedded_reports[0].rating == "增持" and embedded_reports[0].source == "国金证券", str(embedded_reports))

    detail = _mk_detail(financials={"source": "ths", "rows": [
        {"period": "2026H1", "date": "2026-06-30", "revenue": 1.25e9, "revenue_yoy": 25,
         "net_profit": 2.5e8, "net_profit_yoy": 25, "roe": 11.2, "debt_ratio": 40},
    ]})
    fb = analysis.rule_based(detail)
    scores = fb["advice"]["scores"]
    check("财报分析: 基本面加分", scores["fundamental"] > 0 and "2026H1" in fb["fundamental"]["period"], str(fb["fundamental"]))
    check("财报分析: 纳入机会", any("基本面偏强" in str(x) for x in fb["risk"]["opportunities"]), str(fb["risk"]["opportunities"]))
    reversed_detail = _mk_detail(financials={"rows": [
        {"period": "2025FY", "date": "2025-12-31", "revenue_yoy": -20, "net_profit_yoy": -20},
        {"period": "2026H1", "date": "2026-06-30", "revenue_yoy": 10, "net_profit_yoy": 10},
    ]})
    reversed_fb = analysis.rule_based(reversed_detail)
    check("财报分析: 按报告日期取最新", reversed_fb["fundamental"]["period"] == "2026H1", str(reversed_fb["fundamental"]))
    payload = analysis.build_payload(detail)
    fin_payload = payload.get("财报数据_季报中报") or {}
    check("财报分析: LLM投喂报告期", fin_payload.get("最新报告期") == "2026H1", str(fin_payload))
    stale_detail = _mk_detail(financials={"source": "ths", "stale": True, "error": "网页暂不可用", "rows": [
        {"period": "2026H1", "date": "2026-06-30", "revenue_yoy": 10, "net_profit_yoy": 10},
    ]})
    stale_payload = analysis.build_payload(stale_detail)["财报数据_季报中报"]
    stale_fb = analysis.rule_based(stale_detail)
    check("财报分析: 缓存状态投喂", "使用上次成功缓存" in stale_payload.get("数据状态", ""), str(stale_payload))
    check("财报分析: 缓存状态提示", "缓存数据" in stale_fb["fundamental"]["summary"], str(stale_fb["fundamental"]))


def test_rule_precision() -> None:
    # 1) 量能确认：放量上涨 +6 / 放量下跌 -6 / 缩量下跌 +3 / 缩量上涨 -3 / 数据不足 0
    def bars(closes: list, vols: list) -> list:
        return [{"date": f"2026-08-{i+1:02d}", "close": c, "volume": v} for i, (c, v) in enumerate(zip(closes, vols))]

    vol_bull = analysis._volume_confirm(bars([9, 9.1, 9.2, 9.3, 9.4, 9.5], [100, 100, 100, 100, 100, 150]))
    check("量能确认: 放量上涨 +6", vol_bull[0] == 6, str(vol_bull))
    vol_bear = analysis._volume_confirm(bars([9.5, 9.4, 9.3, 9.2, 9.1, 9.0], [100, 100, 100, 100, 100, 150]))
    check("量能确认: 放量下跌 -6", vol_bear[0] == -6, str(vol_bear))
    vol_shrink_dn = analysis._volume_confirm(bars([9.5, 9.4, 9.3, 9.2, 9.1, 9.0], [100, 100, 100, 100, 100, 60]))
    check("量能确认: 缩量下跌 +3", vol_shrink_dn[0] == 3, str(vol_shrink_dn))
    vol_shrink_up = analysis._volume_confirm(bars([9, 9.1, 9.2, 9.3, 9.4, 9.5], [100, 100, 100, 100, 100, 60]))
    check("量能确认: 缩量上涨 -3", vol_shrink_up[0] == -3, str(vol_shrink_up))
    check("量能确认: 数据不足 0", analysis._volume_confirm([])[0] == 0)

    # 2) 乖离修正：价格超 MA20 8% -> 超买风险进 risks + tech 扣分
    detail_over = _mk_detail(
        quote={"code": "600000", "name": "浦发银行", "price": 10.0, "prev_close": 9.9},
        ma=[_ma_item(5, 9.2), _ma_item(10, 9.1), _ma_item(20, 9.0), _ma_item(60, 8.8)],
    )
    fb_over = analysis.rule_based(detail_over)
    check("乖离修正: 超买进风险面", any("超买" in r for r in fb_over["risk"]["risks"]), str(fb_over["risk"]["risks"]))
    detail_low = _mk_detail(
        quote={"code": "600000", "name": "浦发银行", "price": 8.0, "prev_close": 8.1},
        ma=[_ma_item(5, 8.8), _ma_item(10, 8.9), _ma_item(20, 9.0), _ma_item(60, 9.1)],
    )
    fb_low = analysis.rule_based(detail_low)
    check("乖离修正: 超卖进机会面", any("超卖" in o for o in fb_low["risk"]["opportunities"]), str(fb_low["risk"]["opportunities"]))

    # 3) 三维分面明细输出（含当日盘口分项）
    check("分面分数输出", set(fb_over["advice"]["scores"].keys()) == {"tech", "capital", "news", "fundamental", "intraday", "total"},
          str(fb_over["advice"]["scores"]))

    # 4) 信号冲突降档：技术/资金偏空 + 消息强多 -> signal=conflict、降档、置信度压低
    detail_cf = _mk_detail(
        ma=[_ma_item(5, 9.2, "持平"), _ma_item(10, 9.1, "持平"), _ma_item(20, 9.0, "持平"), _ma_item(60, 8.8, "持平")],
        ma_summary={"arrangement": "交织", "above_count": 2, "above": ["MA5", "MA10"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": -1e8, "main_last": -1e8, "main_last5": 0, "streak": 0, "streak_dir": ""}},
        status={"tags": [], "trend": {"chg_20d": -3}},
    )
    news_cf = [{"title": "重大利好" + str(i), "date": f"2026-08-{i+1:02d}",
                 "interpretation": {"sentiment": "利好", "impact": "高", "summary": "s"}} for i in range(2)]
    reports_cf = [{"rating": "买入", "title": "业绩预增" + str(i), "date": f"2026-08-{i+1:02d}",
                   "interpretation": {"sentiment": "利好", "impact": "高", "summary": "s"}} for i in range(2)]
    fb_cf = analysis.rule_based(detail_cf, news_cf, reports_cf)
    check("信号冲突标记", fb_cf["advice"]["signal"] == "conflict", str(fb_cf["advice"]["signal"]))
    check("冲突时提示背离", "背离" in fb_cf["advice"]["reason"], fb_cf["advice"]["reason"])
    check("冲突时不清仓", fb_cf["advice"]["action"] != "清仓离场", fb_cf["advice"]["action"])
    check("冲突时置信度压低", fb_cf["advice"]["confidence"] < 60, str(fb_cf["advice"]["confidence"]))
    # 5) 信号一致增强：技术/资金/消息同向 -> signal=aligned 且置信度上修
    detail_al = _mk_detail(
        ma=[_ma_item(5, 8.6, "上行"), _ma_item(10, 8.5, "上行"), _ma_item(20, 8.4, "上行"), _ma_item(60, 8.2, "上行")],
        ma_summary={"arrangement": "多头排列", "above_count": 4, "above": ["MA5", "MA10", "MA20", "MA60"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 3e8, "main_last": 1e8, "main_last5": 1e8, "streak": 4, "streak_dir": "流入"}},
    )
    fb_al = analysis.rule_based(detail_al, news_cf, reports_cf)
    check("信号一致标记", fb_al["advice"]["signal"] == "aligned", str(fb_al["advice"]["signal"]))
    check("一致时提示共振", "共振" in fb_al["advice"]["reason"], fb_al["advice"]["reason"])
    check("一致时置信度上修", fb_al["advice"]["confidence"] > 80, str(fb_al["advice"]["confidence"]))

    # 5.4) 资金面当日优先：当日流出但 30 日累计流入 -> 判定偏空（与详情页展示一致）
    detail_daily = _mk_detail(
        ma=[_ma_item(5, 9.2, "持平"), _ma_item(10, 9.1, "持平"), _ma_item(20, 9.0, "持平"), _ma_item(60, 8.8, "持平")],
        ma_summary={"arrangement": "交织", "above_count": 2, "above": ["MA5", "MA10"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 15e8, "main_last": -2.45e8, "main_last5": 0.6e8, "streak": 1, "streak_dir": "流出", "fresh": True, "last_date": "2026-08-18"}},
    )
    fb_daily = analysis.rule_based(detail_daily)
    check("资金面当日优先: 当日流出进风险面",
          any("当日主力净流出" in (r if isinstance(r, str) else r.get("text", "")) for r in fb_daily["risk"]["risks"]),
          str(fb_daily["risk"]["risks"]))
    check("资金面当日优先: 不出现累计流入机会",
          not any("近30日主力累计净流入" in (o if isinstance(o, str) else o.get("text", "")) for o in fb_daily["risk"]["opportunities"]),
          str(fb_daily["risk"]["opportunities"]))

    # 5.45) 当日资金流向未发布（16点前，最后一行=昨天）：判定退回近5日口径并标注日期
    detail_nf = _mk_detail(
        ma=[_ma_item(5, 9.2, "持平"), _ma_item(10, 9.1, "持平"), _ma_item(20, 9.0, "持平"), _ma_item(60, 8.8, "持平")],
        ma_summary={"arrangement": "交织", "above_count": 2, "above": ["MA5", "MA10"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 15e8, "main_last": -2.45e8, "main_last5": -0.8e8, "streak": 2, "streak_dir": "流出", "fresh": False, "last_date": "2026-08-17"}},
    )
    fb_nf = analysis.rule_based(detail_nf)
    check("资金未发布: 退回近5日口径",
          any("最近交易日（2026-08-17）" in (r if isinstance(r, str) else r.get("text", "")) for r in fb_nf["risk"]["risks"]),
          str(fb_nf["risk"]["risks"]))
    check("资金未发布: 不把昨日当当日",
          not any("当日主力" in (x if isinstance(x, str) else x.get("text", "")) for x in fb_nf["risk"]["opportunities"] + fb_nf["risk"]["risks"]),
          str(fb_nf["risk"]["risks"]))

    # 5.5) 当日实时盘口数据：趋势/资金段含 intraday，盘口分项计入技术面
    detail_intra = _mk_detail(
        quote={
            "code": "600000", "name": "浦发银行", "price": 9.55, "prev_close": 9.1,
            "change": 0.45, "change_pct": 4.95, "open": 9.2, "high": 9.6, "low": 9.0,
            "volume": 5e7, "amount": 4.8e8, "turnover": 3.2, "volume_ratio": 2.5,
        },
    )
    fb_intra = analysis.rule_based(detail_intra)
    check("当日盘中数据进趋势段", "当日振幅" in fb_intra["trend"].get("intraday", ""), fb_intra["trend"].get("intraday", ""))
    check("盘中位置/量比/换手", "区间" in fb_intra["trend"]["intraday"] and "量比" in fb_intra["trend"]["intraday"]
          and "换手率" in fb_intra["trend"]["intraday"], fb_intra["trend"]["intraday"])
    check("当日资金活跃进资金段", "当日成交额" in fb_intra["capital"].get("intraday", ""), fb_intra["capital"].get("intraday", ""))
    # 高位上涨 + 放量 -> 盘口分项为正、进机会面
    intra_pts = fb_intra["advice"]["scores"]["intraday"]
    check("盘口分项计入技术面", intra_pts > 0, f"intraday={intra_pts}")
    check("盘口分项写进依据", f"盘口 {intra_pts:+d} 分" in fb_intra["advice"]["reason"], fb_intra["advice"]["reason"])
    def _o_txt(x):
        return x if isinstance(x, str) else x.get("text", "")

    check("高位强势提示进机会面", any("高位" in _o_txt(o) and "强势" in _o_txt(o) for o in fb_intra["risk"]["opportunities"]),
          str(fb_intra["risk"]["opportunities"]))
    # 高位回落转跌 -> 盘口分项为负、进风险面（现价贴近当日高点但较昨收下跌）
    detail_intra_bear = _mk_detail(
        quote={
            "code": "600000", "name": "浦发银行", "price": 9.85, "prev_close": 10.0,
            "change": -0.15, "change_pct": -1.5, "open": 9.8, "high": 9.9, "low": 8.9,
            "volume": 5e7, "amount": 4.8e8, "turnover": 3.2, "volume_ratio": 1.2,
        },
    )
    fb_intra_bear = analysis.rule_based(detail_intra_bear)
    intra_bear = fb_intra_bear["advice"]["scores"]["intraday"]
    check("高位回落盘口分项为负", intra_bear < 0, f"intraday={intra_bear}")
    check("冲高回落提示进风险面", any("回落" in _o_txt(r) for r in fb_intra_bear["risk"]["risks"]),
          str(fb_intra_bear["risk"]["risks"]))
    # 数据缺失时 intraday 字段仍存在且为 0、不报错
    fb_no_intra = analysis.rule_based(_mk_detail())
    check("无盘口数据时字段兜底", "intraday" in fb_no_intra["trend"] and "intraday" in fb_no_intra["capital"]
          and fb_no_intra["advice"]["scores"]["intraday"] == 0,
          str(fb_no_intra["trend"].get("intraday")))

    # 5.6) 盘口分项四象限 + 量比/振幅/换手修正
    def _q(**kw) -> dict:
        base = {"code": "600000", "price": 9.4, "prev_close": 9.1, "change_pct": 3.3,
                "open": 9.1, "high": 9.5, "low": 9.0, "volume_ratio": 1.0, "turnover": 2.0}
        base.update(kw)
        return base

    hi_up = analysis._intraday_score(_q(price=9.45, change_pct=3.8))      # 位置 90% + 涨
    hi_dn = analysis._intraday_score(_q(price=9.45, change_pct=-1.1))     # 位置 90% + 跌
    lo_dn = analysis._intraday_score(_q(price=9.05, change_pct=-2.2))     # 位置 10% + 跌
    lo_up = analysis._intraday_score(_q(price=9.05, change_pct=0.6))      # 位置 10% + 涨
    check("盘口四象限: 高位涨 加分", hi_up[0] > 0, str(hi_up))
    check("盘口四象限: 高位跌 减分", hi_dn[0] < 0, str(hi_dn))
    check("盘口四象限: 低位跌 减分", lo_dn[0] < 0, str(lo_dn))
    check("盘口四象限: 低位涨 加分", lo_up[0] > 0, str(lo_up))
    check("盘口: 放量上涨加分", analysis._intraday_score(_q(volume_ratio=2.5))[0] > 3)
    check("盘口: 放量下跌减分", analysis._intraday_score(_q(volume_ratio=2.5, change_pct=-3.3))[0] < -3)
    check("盘口: 缩量削弱信号", analysis._intraday_score(_q(volume_ratio=0.5))[0] < analysis._intraday_score(_q(volume_ratio=1.0))[0])
    check("盘口: 振幅大减分", analysis._intraday_score(_q(high=9.9, low=8.6))[0] < 0)
    check("盘口: 换手极高下跌更空", analysis._intraday_score(_q(turnover=12.0, change_pct=-3.3))[0] < 0)
    check("盘口: 缺数据为 0", analysis._intraday_score({"code": "600000", "price": 9.0})[0] == 0)

    # 5.7) 盘口信号历史命中率强度标注（_annotate_intraday 拆分 + rule_based 输出）
    ann = analysis._annotate_intraday(
        "现价自当日高位回落（92%）转跌，短线抛压显现；量比 2.30 放量下挫，抛压集中释放；"
        "换手率 0.5% 过低，交投清淡"
    )
    check("信号标注: 拆分 3 条", len(ann) == 3, str(ann))
    check("信号标注: 冲高回落=高", ann[0]["strength"] == "高" and "盘中57.1%" in ann[0]["hit"], str(ann[0]))
    check("信号标注: 放量下挫=中样本不足", ann[1]["strength"] == "中" and "样本不足" in ann[1]["hit"], str(ann[1]))
    check("信号标注: 交投清淡=高", ann[2]["strength"] == "高" and "54.3%" in ann[2]["hit"], str(ann[2]))
    check("信号标注: 未匹配子句不标", analysis._annotate_intraday("当日成交额 4.21 亿元，市场交投正常")[0]["strength"] == "",
          str(analysis._annotate_intraday("当日成交额 4.21 亿元，市场交投正常")))
    # 信号置信度：由支撑样本数经 utils.confidence 折算（与自检/回测口径统一）
    ann_c = analysis._annotate_intraday(
        "现价自当日高位回落（92%）转跌，短线抛压显现；换手率 0.5% 过低，交投清淡"
    )
    check("信号置信度: 字段存在", all("confidence" in a for a in ann_c), str(ann_c))
    check("信号置信度: 高样本=高置信", ann_c[1]["confidence"]["level"] == "high"
          and ann_c[1]["confidence"]["label"] == "高", str(ann_c[1]))
    check("信号置信度: 少样本=低置信且与强度独立", ann_c[0]["strength"] == "高" and ann_c[0]["confidence"]["level"] == "low",
          str(ann_c[0]))
    # 口径统一：analysis 标注与 utils.confidence 同函数
    from backend.utils import confidence as _uconf
    check("信号置信度: 口径与 utils 一致",
          ann_c[1]["confidence"] == _uconf(249), str((ann_c[1]["confidence"], _uconf(249))))
    # 盘口机会/风险条目为 dict 结构（带强度），非盘口条目保持字符串
    d_sig = _mk_detail(quote={
        "code": "600000", "name": "浦发银行", "price": 9.85, "prev_close": 10.0,
        "change": -0.15, "change_pct": -1.5, "open": 9.8, "high": 9.9, "low": 8.9,
        "volume": 5e7, "amount": 4.8e8, "turnover": 0.5, "volume_ratio": 2.3,
    })
    # LLM 投喂：payload 含盘口信号可靠性段（强度/命中率/置信度）
    _payload_sig = analysis.build_payload(d_sig).get("盘口信号可靠性_当日") or []
    check("投喂盘口: 段存在且有信号", len(_payload_sig) >= 2, str(_payload_sig)[:200])
    check("投喂盘口: 含强度/命中率/置信度",
          all({"信号", "历史强度", "历史命中率", "置信度"} <= set(s.keys()) for s in _payload_sig),
          str(_payload_sig[0]) if _payload_sig else "无")

    # 7) MACD/KDJ：计算、投喂与规则评分
    from backend.indicators import compute_oscillators
    _obars = []
    _p = 10.0
    for _i in range(60):
        _p += (-0.05 if _i < 30 else 0.08)
        _obars.append(Bar(date=f"2026-08-{_i % 28 + 1:02d}", open=_p - 0.05, close=_p,
                          high=_p + 0.3, low=_p - 0.3, volume=1e6))
    _osc = compute_oscillators(_obars)
    check("MACD/KDJ: 计算产出", _osc["macd"].get("dif") is not None and _osc["kdj"].get("k") is not None,
          str({k: v for k, v in _osc["macd"].items() if k != "series"}))
    check("MACD/KDJ: 数据不足兜底", compute_oscillators(_obars[:20])["macd"] == {} and
          compute_oscillators(_obars[:20])["kdj"] == {}, str(compute_oscillators(_obars[:20])))
    # 摆动指标仅分析展示，不参与评分与结论（当前市场行情下已不适合作为决策数据）
    _d_osc = _mk_detail(quote={"code": "600000", "name": "浦发银行", "price": 10.0,
                                "prev_close": 9.9, "change_pct": 1.0, "high": 10.2,
                                "low": 9.8, "open": 9.95},
                         oscillators=_osc)
    _d_osc2 = _mk_detail(quote={"code": "600000", "name": "浦发银行", "price": 10.0,
                                 "prev_close": 9.9, "change_pct": 1.0, "high": 10.2,
                                 "low": 9.8, "open": 9.95},
                          oscillators={"macd": {"cross": "死叉", "dif": -0.1, "dea": 0.1,
                                                 "hist_trend": "绿柱放大"},
                                       "kdj": {"cross": "死叉", "k": 20, "d": 40, "j": -10, "zone": "超卖"}})
    _fb_osc = analysis.rule_based(_d_osc)
    _fb_osc2 = analysis.rule_based(_d_osc2)
    check("MACD/KDJ: 趋势段含指标行", "MACD" in _fb_osc["trend"].get("oscillators", "") and "KDJ" in _fb_osc["trend"]["oscillators"],
          _fb_osc["trend"].get("oscillators", ""))
    check("MACD/KDJ: 不参与评分",
          _fb_osc["advice"]["scores"]["tech"] == _fb_osc2["advice"]["scores"]["tech"],
          f"金叉 {_fb_osc['advice']['scores']['tech']} vs 死叉 {_fb_osc2['advice']['scores']['tech']}")
    check("MACD/KDJ: 不进机会/风险",
          not any("MACD" in str(x) or "KDJ" in str(x) for x in _fb_osc["risk"]["opportunities"] + _fb_osc["risk"]["risks"]),
          str(_fb_osc["risk"]["opportunities"])[:150])
    check("MACD/KDJ: 投喂段存在", "MACD" in analysis.build_payload(_d_osc)["技术指标_MACD_KDJ"],
          str(analysis.build_payload(_d_osc)["技术指标_MACD_KDJ"])[:200])
    fb_sig = analysis.rule_based(d_sig)
    sig_risks = [x for x in fb_sig["risk"]["risks"] if isinstance(x, dict)]
    check("规则输出: 风险含 dict 标注条目", len(sig_risks) >= 2, str(fb_sig["risk"]["risks"]))
    check("规则输出: dict 条目含 strength/hit",
          all("strength" in x and "hit" in x and "text" in x for x in sig_risks), str(sig_risks))
    check("规则输出: 非盘口条目仍为字符串",
          any(isinstance(x, str) for x in fb_sig["risk"]["risks"]), str(fb_sig["risk"]["risks"]))

    # 6) 三维权重：clamp 越界 + 权重影响分面分
    from backend import scorecfg
    check("权重 clamp 下限", scorecfg._clamp(0.01, 1.0) == 0.2)
    check("权重 clamp 上限", scorecfg._clamp(9.9, 1.0) == 3.0)
    check("权重 clamp 正常值", scorecfg._clamp(1.5, 1.0) == 1.5)
    # 默认权重 1.0 时 score 与分面和一致
    detail_w = _mk_detail(
        ma=[_ma_item(5, 8.6, "上行"), _ma_item(10, 8.5, "上行"), _ma_item(20, 8.4, "上行"), _ma_item(60, 8.2, "上行")],
        ma_summary={"arrangement": "多头排列", "above_count": 4, "above": ["MA5", "MA10", "MA20", "MA60"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 3e8, "main_last": 1e8, "main_last5": 1e8, "streak": 4, "streak_dir": "流入"}},
    )
    fb_w = analysis.rule_based(detail_w)
    s = fb_w["advice"]["scores"]
    check("默认权重下总分=分面和", abs(s["tech"] + s["capital"] + s["news"] - s["total"]) < 0.05, str(s))
    check("权重字段输出", fb_w["advice"]["weights"] == {"tech": 1.0, "capital": 1.0, "news": 1.0}, str(fb_w["advice"]["weights"]))


# ------------------------------------------------------------------ K 线滞后判定
def test_kline_stale() -> None:
    from datetime import datetime
    from backend.utils import kline_is_stale

    # 固定注入时间，避免 CI 在任意时刻运行时判据漂移
    mon_close = datetime(2026, 8, 17, 16, 0)   # 周一收盘后
    # 周一收盘：K线停在周五应判滞后，含今天则正常
    check("K线滞后判定: 周一收盘缺周五->周一(停08-14)", kline_is_stale("2026-08-14", mon_close) is True)
    check("K线滞后判定: 周一收盘已含今天(08-17)", kline_is_stale("2026-08-17", mon_close) is False)
    check("K线滞后判定: 盘中不判定", kline_is_stale("2026-08-14", datetime(2026, 8, 17, 11, 0)) is False)
    check("K线滞后判定: 空日期不滞后", kline_is_stale("", mon_close) is False)
    check("K线滞后判定: 非法日期不滞后", kline_is_stale("abc", mon_close) is False)


# ------------------------------------------------------------------ 缓存

def test_cache() -> None:
    async def run() -> None:
        c = cache.TTLCache()
        calls = {"n": 0}

        async def loader() -> str:
            calls["n"] += 1
            await asyncio.sleep(0.01)
            return "v"

        await asyncio.gather(*[c.get_or_set("k", 60, loader) for _ in range(5)])
        check("缓存单飞（并发只加载一次）", calls["n"] == 1, f"n={calls['n']}")
        check("缓存锁表回收", not c._locks)
        # 过期后应重新加载
        c._data["k"] = (0.0, "stale")
        await c.get_or_set("k", 60, loader)
        check("缓存过期重载", calls["n"] == 2, f"n={calls['n']}")
        # 上限淘汰后不超限
        for i in range(cache.MAX_ENTRIES + 50):
            c.put(f"bulk{i}", i, 100.0)
        check("缓存条目硬上限", len(c._data) <= cache.MAX_ENTRIES, f"n={len(c._data)}")

    asyncio.run(run())


# ------------------------------------------------------------------ AI 每股票单飞锁
def test_ai_lock() -> None:
    async def run() -> None:
        calls = {"n": 0}
        store: dict[str, str] = {}

        async def work() -> str:
            if "report" in store:          # 等锁后二次检查缓存
                return store["report"]
            calls["n"] += 1                 # 真实计算只应执行一次
            await asyncio.sleep(0.01)
            store["report"] = "done"
            return store["report"]

        r = await asyncio.gather(*[api._with_ai_lock("600000", work) for _ in range(5)])
        check("AI 每股票单飞", calls["n"] == 1 and all(x == "done" for x in r), f"n={calls['n']}")
        check("AI 锁表回收", "600000" not in api._ai_locks)

    asyncio.run(run())


def test_ai_cache_freshness() -> None:
    """AI 当日缓存时效：过期快照必须作废重建（保证点击分析时是最新实时数据）。"""
    from datetime import datetime, timedelta

    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    fresh = api._cache_fresh((now - timedelta(seconds=30)).strftime(fmt))
    check("AI 缓存时效: 30s 前快照仍新鲜", fresh is True, f"fresh={fresh}")
    # 2 小时前快照超过盘后 1h TTL，任何时段（盘中 120s / 盘后 1h）都必过期
    stale = api._cache_fresh((now - timedelta(hours=2)).strftime(fmt))
    check("AI 缓存时效: 2 小时前快照过期作废", stale is False, f"stale={stale}")
    bad = api._cache_fresh("not-a-date")
    check("AI 缓存时效: 坏格式快照作废", bad is False, f"bad={bad}")
    # 字段缺失（get_report 无 cached_at）同样视为过期，宁可重建
    none_at = api._cache_fresh("")
    check("AI 缓存时效: 无时间戳作废", none_at is False, f"none={none_at}")


# ------------------------------------------------------------------ 指标
def test_indicators() -> None:
    bars = [
        Bar(date=f"2026-01-{i:02d}", open=10 + i, close=10 + i * 0.5, high=12 + i, low=9 + i, volume=100.0)
        for i in range(1, 70)
    ]
    infos, summary = build_ma(bars, 20.0)
    check("均线 4 条", len(infos) == 4, str(len(infos)))
    check("均线汇总", "arrangement" in summary and "series" in summary)
    ma_values = {i.window: i.value for i in infos}
    sr = support_resistance(bars, 20.0, ma_values)
    check("支撑压力", bool(sr.get("support") and sr.get("resistance")), str(sr.get("state")))

    # 资金流向当日新鲜度：ref_date=K线最新日期
    from backend.providers.base import FlowDay
    flow_rows = [
        FlowDay(date="2026-08-14", main=-1.8e8, sm=0, md=0, lg=0, xl=-1.3e8),
        FlowDay(date="2026-08-17", main=2.4e8, sm=0, md=0, lg=0, xl=2.9e8),
        FlowDay(date="2026-08-18", main=-2.45e8, sm=0, md=0, lg=0, xl=-2.58e8),
    ]
    f_fresh = summarize_flow(flow_rows, ref_date="2026-08-18")
    check("资金新鲜: 当日已发布", f_fresh["fresh"] is True and f_fresh["last_date"] == "2026-08-18", str(f_fresh.get("fresh")))
    check("资金新鲜: 当日口径判定", f_fresh["state"] == "主力净流出", f_fresh["state"])
    # 模拟 16 点前：最后一行是 17 日（昨日）且为流出，K线已到 18 日
    flow_y = [
        FlowDay(date="2026-08-14", main=1.8e8, sm=0, md=0, lg=0, xl=1.3e8),
        FlowDay(date="2026-08-17", main=-2.4e8, sm=0, md=0, lg=0, xl=-2.9e8),
    ]
    f_y = summarize_flow(flow_y, ref_date="2026-08-18")
    check("资金未发布: fresh=False", f_y["fresh"] is False and f_y["last_date"] == "2026-08-17", str(f_y.get("fresh")))
    check("资金未发布: 退回近5日口径", f_y["state"] == "主力净流出（近5日）", f_y["state"])


# ------------------------------------------------------------------ 数据源装配（不触网）
def test_registry() -> None:
    reg = registry()
    names = [p.name for p in reg.providers]
    check("数据源装配", "eastmoney" in names and len(names) >= 3, str(names))
    check("健康度接口（不触网）", len(reg.health()) == len(names))
    # 资讯/研报/财报能力：同花顺主源，东方财富辅源，资讯保留新浪末级兜底
    news_caps = sorted(p.name for p in reg.providers if "news" in p.caps)
    report_caps = sorted(p.name for p in reg.providers if "reports" in p.caps)
    financial_caps = sorted(p.name for p in reg.providers if "financials" in p.caps)
    check("资讯源装配（同花顺+东财+新浪兜底）", news_caps == ["eastmoney", "sina", "ths"], str(news_caps))
    check("研报源装配（同花顺+东财兜底）", report_caps == ["eastmoney", "ths"], str(report_caps))
    check("财报源装配（同花顺+东财兜底）", financial_caps == ["eastmoney", "ths"], str(financial_caps))
    ordered_news = [p.name for p in reg._available("news")]
    ordered_reports = [p.name for p in reg._available("reports")]
    check("资讯研报优先级: 同花顺在东财前", ordered_news[:2] == ["ths", "eastmoney"] and ordered_reports[:2] == ["ths", "eastmoney"],
          str((ordered_news, ordered_reports)))


def test_watch_monitor() -> None:
    add = service.watch_monitor({"status": "normal", "change_pct": 3.2, "volume_ratio": 1.8})
    reduce = service.watch_monitor({"status": "normal", "change_pct": -3.0, "volume_ratio": 1.0})
    observe = service.watch_monitor({"status": "normal", "change_pct": 1.2, "volume_ratio": 1.0})
    delayed = service.watch_monitor({"status": "delayed", "status_text": "数据更新延迟"})
    check("关键监测: 放量上涨提示可加仓", add["action"] == "可加仓" and add["tone"] == "up", str(add))
    check("关键监测: 下跌提示应减仓", reduce["action"] == "应减仓" and reduce["tone"] == "down", str(reduce))
    check("关键监测: 普通波动继续观察", observe["action"] == "继续观察", str(observe))
    check("关键监测: 异常行情不误报加减仓", delayed["action"] == "继续观察" and delayed["tone"] == "warn", str(delayed))
    old_trading, old_session = service.is_trading_now, service.session_state
    try:
        service.is_trading_now = lambda: True
        service.session_state = lambda: "open"
        check("首页刷新周期: 5秒", service.session_info()["interval_ms"] == 5000)
    finally:
        service.is_trading_now, service.session_state = old_trading, old_session


def test_watch_monitor() -> None:
    add = service.watch_monitor({"status": "normal", "change_pct": 3.2, "volume_ratio": 1.8})
    reduce = service.watch_monitor({"status": "normal", "change_pct": -3.0, "volume_ratio": 1.0})
    observe = service.watch_monitor({"status": "normal", "change_pct": 1.2, "volume_ratio": 1.0})
    delayed = service.watch_monitor({"status": "delayed", "status_text": "数据更新延迟"})
    check("关键监测: 放量上涨提示可加仓", add["action"] == "可加仓" and add["tone"] == "up", str(add))
    check("关键监测: 下跌提示应减仓", reduce["action"] == "应减仓" and reduce["tone"] == "down", str(reduce))
    check("关键监测: 普通波动继续观察", observe["action"] == "继续观察", str(observe))
    check("关键监测: 异常行情不误报加减仓", delayed["action"] == "继续观察" and delayed["tone"] == "warn", str(delayed))
    old_trading, old_session = service.is_trading_now, service.session_state
    try:
        service.is_trading_now = lambda: True
        service.session_state = lambda: "open"
        check("首页刷新周期: 5秒", service.session_info()["interval_ms"] == 5000)
    finally:
        service.is_trading_now, service.session_state = old_trading, old_session


def test_items_fingerprint() -> None:
    """解读缓存指纹：条目内容一变指纹即变，防止新标题配旧解读的缓存错位。"""
    from backend.utils import items_fingerprint

    a = [{"id": "1", "date": "2026-08-18", "title": "中标合同"},
         {"id": "2", "date": "2026-08-17", "title": "回购"}]
    b = [{"id": "2", "date": "2026-08-17", "title": "回购"},
         {"id": "3", "date": "2026-08-16", "title": "减持"}]
    check("指纹: 相同条目同指纹", items_fingerprint(a) == items_fingerprint(list(a)), items_fingerprint(a))
    check("指纹: 条目变化指纹变化", items_fingerprint(a) != items_fingerprint(b),
          f"{items_fingerprint(a)} vs {items_fingerprint(b)}")
    check("指纹: 标题变化指纹变化",
          items_fingerprint([{"id": "1", "title": "A"}]) != items_fingerprint([{"id": "1", "title": "B"}]))
    collision_a = [{"id": "1", "date": "2026:08", "title": "18"}]
    collision_b = [{"id": "1", "date": "2026", "title": "08:18"}]
    check("指纹: 字段分隔符不会碰撞", items_fingerprint(collision_a) != items_fingerprint(collision_b))
    check("指纹: 空列表稳定不报错", isinstance(items_fingerprint([]), str) and len(items_fingerprint([])) == 12)


# ------------------------------------------------------------------ 盘口回测脚本自测（不触网，合成日线）
def test_backtest_selftest() -> None:
    import subprocess
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            [_sys.executable, str(root / "backtest_intraday.py"), "--selftest"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        detail = (proc.stdout or "")[-200:] + (proc.stderr or "")[-200:]
    except subprocess.TimeoutExpired as exc:
        check("盘口回测脚本自测", False, f"超时: {exc}")
        return
    check("盘口回测脚本自测", proc.returncode == 0, detail)

    # 盘中 vs 收盘对照实验脚本自测（不触网，合成日线+分钟线）
    try:
        proc2 = subprocess.run(
            [_sys.executable, str(root / "backtest_compare.py"), "--selftest"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        detail2 = (proc2.stdout or "")[-200:] + (proc2.stderr or "")[-200:]
    except subprocess.TimeoutExpired as exc:
        check("盘中对照实验脚本自测", False, f"超时: {exc}")
        return
    check("盘中对照实验脚本自测", proc2.returncode == 0, detail2)


# ------------------------------------------------------------------ 自检报告含回测段落（不触网，仅结构）
def test_check_sources_backtest_struct() -> None:
    from backend import check_sources

    # _backtest_advice 口径与回测脚本一致
    check("回测校准建议: 有效可上调", check_sources._backtest_advice(0.60, 0.46, 200) == "有效，可维持或上调权重")
    check("回测校准建议: 有效可维持", check_sources._backtest_advice(0.49, 0.46, 200) == "有效，权重可维持")
    check("回测校准建议: 偏弱下调", check_sources._backtest_advice(0.44, 0.46, 200) == "偏弱，建议下调权重")
    check("回测校准建议: 反向", check_sources._backtest_advice(0.38, 0.46, 200) == "反向/无效，建议大幅下调或检查方向")
    check("回测校准建议: 样本不足", check_sources._backtest_advice(0.90, 0.46, 10) == "样本不足，暂不调整")

    # check_backtest 失败分支（空样本）返回 ok=False 且不抛错
    async def _probe_empty() -> dict:
        return await check_sources.check_backtest([("600000", "SH")], days=5)

    res = asyncio.run(_probe_empty())
    check("回测探测: 样本不足返回 ok=False", res.get("ok") is False, str(res))

    # 回测脚本缺失降级分支：镜像未打包 backtest_intraday.py 时返回 degraded
    # 提示而非抛异常（容器运行时场景，不触网）
    orig_import = check_sources.__dict__.get("__bt_import_guard")
    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "backtest_intraday":
            raise ModuleNotFoundError("No module named 'backtest_intraday'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _fake_import
    try:
        res_d = asyncio.run(check_sources.check_backtest([("600000", "SH")], days=5))
    finally:
        builtins.__import__ = real_import
    check("回测探测: 脚本缺失降级不抛错",
          res_d.get("ok") is False and res_d.get("degraded") is True and "未打包" in res_d.get("error", ""),
          str(res_d))

    # 渲染函数能处理含回测段/无回测段的报告
    text_with = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": ["tencent"],
        "latest_trade_date": "2026-08-17", "issues": [],
        "backtest": {
            "ok": True, "samples": 100, "stocks": 1, "base_up_rate": 46.0,
            "base_avg_ret": 0.1, "buckets": [], "signals": [],
        },
    })
    check("自检渲染: 含回测标题", "盘口信号近期命中率" in text_with, text_with[:200])
    text_no = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": [],
        "latest_trade_date": "", "issues": [], "backtest": {"ok": False, "error": "回测失败: x"},
    })
    check("自检渲染: 回测失败分支", "回测失败" in text_no)
    text_dg = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": ["tencent"],
        "latest_trade_date": "", "issues": [],
        "backtest": {"ok": False, "error": "回测脚本未打包进镜像（ModuleNotFoundError）", "degraded": True},
    })
    check("自检渲染: 回测降级分支", "已降级" in text_dg and "其余自检正常" in text_dg, text_dg[:300])
    text_sk = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": ["tencent"],
        "latest_trade_date": "", "issues": [],
        "backtest": {"ok": False, "skipped": True, "error": "已跳过回测（仅数据源自检）"},
    })
    check("自检渲染: 回测跳过分支", "已跳过回测" in text_sk, text_sk[:300])
    # run_diagnostics 分离：with_backtest=False 时返回 skipped 且不执行回测
    report_fast = asyncio.run(check_sources.run_diagnostics("600000", with_backtest=False))
    check("自检分离: 仅数据源跳过回测", report_fast["backtest"].get("skipped") is True
          and report_fast["backtest_days"] == 0,
          str(report_fast["backtest"])[:120])
    # 回测深度参数化：with_backtest=True 时 backtest_days 传递并记录
    report_days = asyncio.run(check_sources.run_diagnostics("600000", with_backtest=True,
                                                             backtest_days=30))
    check("回测深度: backtest_days 传递", report_days.get("backtest_days") == 30,
          str(report_days.get("backtest_days")))

    # 置信度分档：样本越深越可靠
    check("置信度: ≥100 高", check_sources._confidence(500)["level"] == "high",
          str(check_sources._confidence(500)))
    check("置信度: 50-99 中", check_sources._confidence(60)["level"] == "medium",
          str(check_sources._confidence(60)))
    check("置信度: <50 低", check_sources._confidence(10)["level"] == "low",
          str(check_sources._confidence(10)))
    # 汇总报告三层置信度字段（总体/分桶/信号）
    conf_samples = [
        {"score": 3, "next_ret": 1.2, "labels": [("高位强势", True)]},
        {"score": -2, "next_ret": -0.5, "labels": [("低位下跌", False)]},
        {"score": 0, "next_ret": 0.3, "labels": []},
    ] * 20
    conf_rep = check_sources._summarize_backtest_safe(
        {"samples": conf_samples, "per_stock": [{"code": "600000"}]}, [("600000", "SH")]
    )
    check("置信度: 报告含总体字段", conf_rep["confidence"]["level"] == "low", str(conf_rep["confidence"]))
    check("置信度: 分桶含字段", all("confidence" in b for b in conf_rep["buckets"]), str(conf_rep["buckets"][:1]))
    check("置信度: 信号含字段", all("confidence" in s for s in conf_rep["signals"]), str(conf_rep["signals"][:1]))

    # 独立回测脚本同步置信度：confidence 分档与 render 报告含置信列（不触网）
    import backtest_intraday as _bt
    check("脚本置信度: 分档一致", _bt.confidence(500)[0] == "高" and _bt.confidence(60)[0] == "中"
          and _bt.confidence(10)[0] == "低", str((_bt.confidence(500), _bt.confidence(60), _bt.confidence(10))))
    _bt_report = _bt.render({
        "per_stock": [{"code": "600000"}],
        "samples": [
            {"score": 3, "next_ret": 1.2, "labels": [("高位强势", True)]},
            {"score": -2, "next_ret": -0.5, "labels": [("低位下跌", False)]},
            {"score": 0, "next_ret": 0.3, "labels": []},
        ] * 40,
    })
    check("脚本置信度: 报告含总体/列", "置信度:" in _bt_report and "置信" in _bt_report,
          _bt_report[:200])

    # _summarize_backtest_safe 兜底：异常样本结构不抛错，返回 ok=False
    bad_summary = check_sources._summarize_backtest_safe(
        {"samples": [{"score": 3} for _ in range(40)], "per_stock": []}, [("600000", "SH")]
    )
    check("回测统计兜底: 坏样本不抛错", bad_summary.get("ok") is False and "回测统计失败" in bad_summary.get("error", ""),
          str(bad_summary))
    # 正常样本走通统计
    ok_samples = [
        {"score": 3, "next_ret": 1.2, "labels": [("高位强势", True)]},
        {"score": -2, "next_ret": -0.5, "labels": [("低位下跌", False)]},
        {"score": 0, "next_ret": 0.3, "labels": []},
    ] * 20
    good_summary = check_sources._summarize_backtest_safe(
        {"samples": ok_samples, "per_stock": [{"code": "600000"}]}, [("600000", "SH")]
    )
    check("回测统计兜底: 正常样本可出报告", good_summary.get("ok") is True and good_summary["samples"] == 60,
          str(good_summary)[:200])


def main() -> int:
    test_json_repair()
    test_fingerprint()
    test_model_filter()
    test_news_interpret()
    test_reports_interpret()
    test_financials()
    test_rule_precision()
    test_kline_stale()
    test_cache()
    test_ai_lock()
    test_ai_cache_freshness()
    test_indicators()
    test_registry()
    test_watch_monitor()
    test_items_fingerprint()
    test_backtest_selftest()
    test_check_sources_backtest_struct()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 共 {TOTAL} 项检查: {FAILED}")
        return 1
    print(f"全部通过（{TOTAL} 项检查）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
