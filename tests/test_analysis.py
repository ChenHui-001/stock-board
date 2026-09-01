"""Analysis。"""
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

def test_rule_precision() -> None:
    # 1) 量能确认：放量上涨 +6 / 放量下跌 -6 / 缩量下跌 +3 / 缩量上涨 -3 / 数据不足 0
    def bars(closes: list, vols: list) -> list:
        return [{"date": f"2026-08-{i+1:02d}", "close": c, "volume": v} for i, (c, v) in enumerate(zip(closes, vols))]

    vol_bull = analysis._volume_confirm(bars([9, 9.1, 9.2, 9.3, 9.4, 9.5], [100, 100, 100, 100, 100, 150]))
    assert (vol_bull[0] == 6), str(vol_bull)
    vol_bear = analysis._volume_confirm(bars([9.5, 9.4, 9.3, 9.2, 9.1, 9.0], [100, 100, 100, 100, 100, 150]))
    assert (vol_bear[0] == -6), str(vol_bear)
    vol_shrink_dn = analysis._volume_confirm(bars([9.5, 9.4, 9.3, 9.2, 9.1, 9.0], [100, 100, 100, 100, 100, 60]))
    assert (vol_shrink_dn[0] == 3), str(vol_shrink_dn)
    vol_shrink_up = analysis._volume_confirm(bars([9, 9.1, 9.2, 9.3, 9.4, 9.5], [100, 100, 100, 100, 100, 60]))
    assert (vol_shrink_up[0] == -3), str(vol_shrink_up)
    assert (analysis._volume_confirm([])[0] == 0)

    # 1.1) 地量股放量阈值上调：近5日均量远低于60日均量时，量比 1.4 不再判放量
    # 构造 60 根 bar：前 55 根均量 1000（高位），近 5 根均量 100（地量），
    # 最后一日量 140（相对近5日均量的 1.4 倍，但绝对量仍远低于历史均值）
    def bars60() -> list:
        rows = []
        for i in range(60):
            # 前 54 根高位量 1000，近 5 根地量 100，最后一天 140
            vol = 1000 if i < 54 else (100 if i < 59 else 140)
            close = 9.0 + (i - 54) * 0.2  # 近 5 日上涨
            rows.append({"date": f"2026-{i//30+1:02d}-{i%30+1:02d}", "close": close, "volume": vol})
        return rows

    # 近5日均量 = (100*5)/5 = 100；60日均量 = (1000*54 + 100*5)/60 ≈ 908
    # 100 < 908*0.7=636 → 地量股，阈值上调到 1.5；量比 140/100=1.4 不达标 → 0
    assert (analysis._volume_confirm(bars60())[0] == 0), str(analysis._volume_confirm(bars60()))

    # 2) 乖离修正：价格超 MA20 8% -> 超买风险进 risks + tech 扣分
    detail_over = _mk_detail(
        quote={"code": "600000", "name": "浦发银行", "price": 10.0, "prev_close": 9.9},
        ma=[_ma_item(5, 9.2), _ma_item(10, 9.1), _ma_item(20, 9.0), _ma_item(60, 8.8)],
    )
    fb_over = analysis.rule_based(detail_over)
    assert (any("超买" in r for r in fb_over["risk"]["risks"])), str(fb_over["risk"]["risks"])
    detail_low = _mk_detail(
        quote={"code": "600000", "name": "浦发银行", "price": 8.0, "prev_close": 8.1},
        ma=[_ma_item(5, 8.8), _ma_item(10, 8.9), _ma_item(20, 9.0), _ma_item(60, 9.1)],
    )
    fb_low = analysis.rule_based(detail_low)
    assert (any("超卖" in o for o in fb_low["risk"]["opportunities"])), str(fb_low["risk"]["opportunities"])

    # 3) 三维分面明细输出（含当日盘口分项）
    assert (set(fb_over["advice"]["scores"].keys()) == {"tech", "capital", "news", "fundamental", "intraday", "total"}), str(fb_over["advice"]["scores"])

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
    assert (fb_cf["advice"]["signal"] == "conflict"), str(fb_cf["advice"]["signal"])
    assert ("背离" in fb_cf["advice"]["reason"]), fb_cf["advice"]["reason"]
    assert (fb_cf["advice"]["action"] != "清仓离场"), fb_cf["advice"]["action"]
    assert (fb_cf["advice"]["confidence"] < 60), str(fb_cf["advice"]["confidence"])
    # 5) 信号一致增强：技术/资金/消息同向 -> signal=aligned 且置信度上修
    detail_al = _mk_detail(
        ma=[_ma_item(5, 8.6, "上行"), _ma_item(10, 8.5, "上行"), _ma_item(20, 8.4, "上行"), _ma_item(60, 8.2, "上行")],
        ma_summary={"arrangement": "多头排列", "above_count": 4, "above": ["MA5", "MA10", "MA20", "MA60"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 3e8, "main_last": 1e8, "main_last5": 1e8, "streak": 4, "streak_dir": "流入"}},
    )
    fb_al = analysis.rule_based(detail_al, news_cf, reports_cf)
    assert (fb_al["advice"]["signal"] == "aligned"), str(fb_al["advice"]["signal"])
    assert ("共振" in fb_al["advice"]["reason"]), fb_al["advice"]["reason"]
    # 置信度量表已按因子归因结论收敛：原 45~92/基础 68 → 现 35~78/基础 50。
    # 依据：回测显示总分 IC 在 7 个半年期里 0/7 为正，加仓档胜率 48.4% 反低于清仓档 50.7%，
    # 旧公式会给一个方向已被证伪的评分输出 90+ 置信度。故原断言 >80 在新量表下不可达（上限 78），
    # 改为断言「共振落在量表顶部区间」+「共振与背离的置信度差 >=30」，保留原测试意图。
    assert (fb_al["advice"]["confidence"] > 70), str(fb_al["advice"]["confidence"])
    assert (fb_al["advice"]["confidence"] - fb_cf["advice"]["confidence"] >= 30), (
        str(fb_al["advice"]["confidence"]), str(fb_cf["advice"]["confidence"]))

    # 5.4) 资金面当日优先：当日流出但 30 日累计流入 -> 判定偏空（与详情页展示一致）
    detail_daily = _mk_detail(
        ma=[_ma_item(5, 9.2, "持平"), _ma_item(10, 9.1, "持平"), _ma_item(20, 9.0, "持平"), _ma_item(60, 8.8, "持平")],
        ma_summary={"arrangement": "交织", "above_count": 2, "above": ["MA5", "MA10"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 15e8, "main_last": -2.45e8, "main_last5": 0.6e8, "streak": 1, "streak_dir": "流出", "fresh": True, "last_date": "2026-08-18"}},
    )
    fb_daily = analysis.rule_based(detail_daily)
    assert (any("当日主力净流出" in (r if isinstance(r, str) else r.get("text", "")) for r in fb_daily["risk"]["risks"])), str(fb_daily["risk"]["risks"])
    assert (not any("近30日主力累计净流入" in (o if isinstance(o, str) else o.get("text", "")) for o in fb_daily["risk"]["opportunities"])), str(fb_daily["risk"]["opportunities"])

    # 5.45) 当日资金流向未发布（16点前，最后一行=昨天）：判定退回近5日口径并标注日期
    detail_nf = _mk_detail(
        ma=[_ma_item(5, 9.2, "持平"), _ma_item(10, 9.1, "持平"), _ma_item(20, 9.0, "持平"), _ma_item(60, 8.8, "持平")],
        ma_summary={"arrangement": "交织", "above_count": 2, "above": ["MA5", "MA10"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 15e8, "main_last": -2.45e8, "main_last5": -0.8e8, "streak": 2, "streak_dir": "流出", "fresh": False, "last_date": "2026-08-17"}},
    )
    fb_nf = analysis.rule_based(detail_nf)
    assert (any("最近交易日（2026-08-17）" in (r if isinstance(r, str) else r.get("text", "")) for r in fb_nf["risk"]["risks"])), str(fb_nf["risk"]["risks"])
    assert (not any("当日主力" in (x if isinstance(x, str) else x.get("text", "")) for x in fb_nf["risk"]["opportunities"] + fb_nf["risk"]["risks"])), str(fb_nf["risk"]["risks"])

    # 5.5) 当日实时盘口数据：趋势/资金段含 intraday，盘口分项计入技术面
    detail_intra = _mk_detail(
        quote={
            "code": "600000", "name": "浦发银行", "price": 9.55, "prev_close": 9.1,
            "change": 0.45, "change_pct": 4.95, "open": 9.2, "high": 9.6, "low": 9.0,
            "volume": 5e7, "amount": 4.8e8, "turnover": 3.2, "volume_ratio": 2.5,
        },
    )
    fb_intra = analysis.rule_based(detail_intra)
    assert ("当日振幅" in fb_intra["trend"].get("intraday", "")), fb_intra["trend"].get("intraday", "")
    assert ("区间" in fb_intra["trend"]["intraday"] and "量比" in fb_intra["trend"]["intraday"]
          and "换手率" in fb_intra["trend"]["intraday"]), fb_intra["trend"]["intraday"]
    assert ("当日成交额" in fb_intra["capital"].get("intraday", "")), fb_intra["capital"].get("intraday", "")
    # 高位上涨 + 放量 -> 盘口分项为正、进机会面
    intra_pts = fb_intra["advice"]["scores"]["intraday"]
    assert (intra_pts > 0), f"intraday={intra_pts}"
    assert (f"盘口 {intra_pts:+d} 分" in fb_intra["advice"]["reason"]), fb_intra["advice"]["reason"]
    def _o_txt(x):
        return x if isinstance(x, str) else x.get("text", "")

    assert (any("高位" in _o_txt(o) and "强势" in _o_txt(o) for o in fb_intra["risk"]["opportunities"])), str(fb_intra["risk"]["opportunities"])
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
    assert (intra_bear < 0), f"intraday={intra_bear}"
    assert (any("回落" in _o_txt(r) for r in fb_intra_bear["risk"]["risks"])), str(fb_intra_bear["risk"]["risks"])
    # 数据缺失时 intraday 字段仍存在且为 0、不报错
    fb_no_intra = analysis.rule_based(_mk_detail())
    assert ("intraday" in fb_no_intra["trend"] and "intraday" in fb_no_intra["capital"]
          and fb_no_intra["advice"]["scores"]["intraday"] == 0), str(fb_no_intra["trend"].get("intraday"))

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
    assert (hi_up[0] > 0), str(hi_up)
    assert (hi_dn[0] < 0), str(hi_dn)
    assert (lo_dn[0] < 0), str(lo_dn)
    assert (lo_up[0] > 0), str(lo_up)
    assert (analysis._intraday_score(_q(volume_ratio=2.5))[0] > 3)
    assert (analysis._intraday_score(_q(volume_ratio=2.5, change_pct=-3.3))[0] < -3)
    assert (analysis._intraday_score(_q(volume_ratio=0.5))[0] < analysis._intraday_score(_q(volume_ratio=1.0))[0])
    assert (analysis._intraday_score(_q(high=9.9, low=8.6))[0] < 0)
    assert (analysis._intraday_score(_q(turnover=12.0, change_pct=-3.3))[0] < 0)
    assert (analysis._intraday_score({"code": "600000", "price": 9.0})[0] == 0)

    # 5.7) 盘口信号历史命中率强度标注（_annotate_intraday 拆分 + rule_based 输出）
    ann = analysis._annotate_intraday(
        "现价自当日高位回落（92%）转跌，短线抛压显现；量比 2.30 放量下挫，抛压集中释放；"
        "换手率 0.5% 过低，交投清淡"
    )
    assert (len(ann) == 3), str(ann)
    assert (ann[0]["strength"] == "高" and "盘中57.1%" in ann[0]["hit"]), str(ann[0])
    assert (ann[1]["strength"] == "中" and "样本不足" in ann[1]["hit"]), str(ann[1])
    assert (ann[2]["strength"] == "高" and "54.3%" in ann[2]["hit"]), str(ann[2])
    assert (analysis._annotate_intraday("当日成交额 4.21 亿元，市场交投正常")[0]["strength"] == ""), str(analysis._annotate_intraday("当日成交额 4.21 亿元，市场交投正常"))
    # 信号置信度：由支撑样本数经 utils.confidence 折算（与自检/回测口径统一）
    ann_c = analysis._annotate_intraday(
        "现价自当日高位回落（92%）转跌，短线抛压显现；换手率 0.5% 过低，交投清淡"
    )
    assert (all("confidence" in a for a in ann_c)), str(ann_c)
    assert (ann_c[1]["confidence"]["level"] == "high"
          and ann_c[1]["confidence"]["label"] == "高"), str(ann_c[1])
    assert (ann_c[0]["strength"] == "高" and ann_c[0]["confidence"]["level"] == "low"), str(ann_c[0])
    # 口径统一：analysis 标注与 utils.confidence 同函数
    from backend.utils import confidence as _uconf
    assert (ann_c[1]["confidence"] == _uconf(249)), str((ann_c[1]["confidence"], _uconf(249)))
    # 盘口机会/风险条目为 dict 结构（带强度），非盘口条目保持字符串
    d_sig = _mk_detail(quote={
        "code": "600000", "name": "浦发银行", "price": 9.85, "prev_close": 10.0,
        "change": -0.15, "change_pct": -1.5, "open": 9.8, "high": 9.9, "low": 8.9,
        "volume": 5e7, "amount": 4.8e8, "turnover": 0.5, "volume_ratio": 2.3,
    })
    # LLM 投喂：payload 含盘口信号可靠性段（强度/命中率/置信度）
    _payload_sig = analysis.build_payload(d_sig).get("盘口信号可靠性_当日") or []
    assert (len(_payload_sig) >= 2), str(_payload_sig)[:200]
    assert (all({"信号", "历史强度", "历史命中率", "置信度"} <= set(s.keys()) for s in _payload_sig)), str(_payload_sig[0]) if _payload_sig else "无"

    # 7) MACD/KDJ：计算、投喂与规则评分
    from backend.indicators import compute_oscillators
    _obars = []
    _p = 10.0
    for _i in range(60):
        _p += (-0.05 if _i < 30 else 0.08)
        _obars.append(Bar(date=f"2026-08-{_i % 28 + 1:02d}", open=_p - 0.05, close=_p,
                          high=_p + 0.3, low=_p - 0.3, volume=1e6))
    _osc = compute_oscillators(_obars)
    assert (_osc["macd"].get("dif") is not None and _osc["kdj"].get("k") is not None), str({k: v for k, v in _osc["macd"].items() if k != "series"})
    assert (compute_oscillators(_obars[:20])["macd"] == {} and
          compute_oscillators(_obars[:20])["kdj"] == {}), str(compute_oscillators(_obars[:20]))
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
    assert ("MACD" in _fb_osc["trend"].get("oscillators", "") and "KDJ" in _fb_osc["trend"]["oscillators"]), _fb_osc["trend"].get("oscillators", "")
    assert (_fb_osc["advice"]["scores"]["tech"] == _fb_osc2["advice"]["scores"]["tech"]), f"金叉 {_fb_osc['advice']['scores']['tech']} vs 死叉 {_fb_osc2['advice']['scores']['tech']}"
    assert (not any("MACD" in str(x) or "KDJ" in str(x) for x in _fb_osc["risk"]["opportunities"] + _fb_osc["risk"]["risks"])), str(_fb_osc["risk"]["opportunities"])[:150]
    assert ("MACD" in analysis.build_payload(_d_osc)["技术指标_MACD_KDJ"]), str(analysis.build_payload(_d_osc)["技术指标_MACD_KDJ"])[:200]
    fb_sig = analysis.rule_based(d_sig)
    sig_risks = [x for x in fb_sig["risk"]["risks"] if isinstance(x, dict)]
    assert (len(sig_risks) >= 2), str(fb_sig["risk"]["risks"])
    assert (all("strength" in x and "hit" in x and "text" in x for x in sig_risks)), str(sig_risks)
    assert (any(isinstance(x, str) for x in fb_sig["risk"]["risks"])), str(fb_sig["risk"]["risks"])

    # 6) 三维权重：clamp 越界 + 权重影响分面分
    from backend import scorecfg
    assert (scorecfg._clamp(0.01, 1.0) == 0.2)
    assert (scorecfg._clamp(9.9, 1.0) == 3.0)
    assert (scorecfg._clamp(1.5, 1.0) == 1.5)
    # 默认权重 1.0 时 score 与分面和一致
    detail_w = _mk_detail(
        ma=[_ma_item(5, 8.6, "上行"), _ma_item(10, 8.5, "上行"), _ma_item(20, 8.4, "上行"), _ma_item(60, 8.2, "上行")],
        ma_summary={"arrangement": "多头排列", "above_count": 4, "above": ["MA5", "MA10", "MA20", "MA60"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 3e8, "main_last": 1e8, "main_last5": 1e8, "streak": 4, "streak_dir": "流入"}},
    )
    fb_w = analysis.rule_based(detail_w)
    s = fb_w["advice"]["scores"]
    assert (abs(s["tech"] + s["capital"] + s["news"] - s["total"]) < 0.05), str(s)
    assert (fb_w["advice"]["weights"] == {"tech": 1.0, "capital": 1.0, "news": 1.0}), str(fb_w["advice"]["weights"])





def test_ai_sanitize() -> None:
    # 构造一个「真实可用」的 fallback：作为兜底值参考基准
    detail_ok = _mk_detail(
        quote={"code": "600000", "name": "浦发银行", "price": 10.0, "prev_close": 9.9,
               "change_pct": 1.01, "open": 9.95, "high": 10.1, "low": 9.85},
        ma=[_ma_item(5, 9.8), _ma_item(10, 9.7), _ma_item(20, 9.5), _ma_item(60, 9.0)],
        ma_summary={"arrangement": "多头排列", "above_count": 4,
                    "above": ["MA5", "MA10", "MA20", "MA60"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 1e8, "main_last": 0.5e8,
                                          "main_last5": 0.3e8, "streak": 3, "streak_dir": "流入"}},
    )
    fb = analysis.rule_based(detail_ok)
    price = 10.0  # 与 quote.price 对齐，用于价位越界判断

    # 1) LLM 返回完整且合规：直接通过（无回退）
    out_ok = analysis._sanitize(fb, fb, price)
    assert (out_ok["advice"]["action"] == fb["advice"]["action"]), str(out_ok["advice"]["action"])
    assert (out_ok["advice"]["support"] == round(fb["advice"]["support"], 2)), str(out_ok["advice"]["support"])

    # 2) action 子串模糊匹配：「继续持有」含「持有」→ 命中「持有观望」（ACTIONS = [积极持仓/加仓, 持有观望, 减仓规避, 清仓离场]）
    llm_substr = {**fb, "advice": {**fb["advice"], "action": "继续持有"}}
    out_substr = analysis._sanitize(llm_substr, fb, price)
    assert (out_substr["advice"]["action"] == "持有观望"), str(out_substr["advice"]["action"])
    assert ("action_note" not in out_substr["advice"]), str(out_substr["advice"])

    # 2.1) action 完全不在 4 选 1（前 2 字不在 ACTIONS 任何项里）：回退规则值 + action_note 标注
    # 「减持规避」含「减持」但 ACTIONS 里只有「减仓」 → 模糊匹配失败
    llm_no_match = {**fb, "advice": {**fb["advice"], "action": "减持规避"}}
    out_nm = analysis._sanitize(llm_no_match, fb, price)
    assert (out_nm["advice"]["action"] == fb["advice"]["action"]), str(out_nm["advice"]["action"])
    assert ("action_note" in out_nm["advice"] and "减持规避" in out_nm["advice"]["action_note"]), str(out_nm["advice"].get("action_note"))

    # 3) 价位越界：support=1.0（现价 10 的 10%）→ 回退；resistance=50（5 倍现价）→ 回退
    llm_bad_levels = {**fb, "advice": {
        **fb["advice"],
        "support": 1.0, "resistance": 50.0, "stop_loss": 0, "take_profit": -5,
    }}
    out_lvl = analysis._sanitize(llm_bad_levels, fb, price)
    # 现价 10，±50% 区间 = [5, 15]
    assert (out_lvl["advice"]["support"] == fb["advice"]["support"]), str(out_lvl["advice"]["support"])
    assert (out_lvl["advice"]["resistance"] == fb["advice"]["resistance"]), str(out_lvl["advice"]["resistance"])
    assert (out_lvl["advice"]["take_profit"] == fb["advice"]["take_profit"]), str(out_lvl["advice"]["take_profit"])
    assert (out_lvl["advice"]["stop_loss"] == fb["advice"]["stop_loss"]), str(out_lvl["advice"]["stop_loss"])

    # 3.0) 价位在合理区间：应保留并保留 2 位小数
    fb_support = fb["advice"]["support"]
    fb_resistance = fb["advice"]["resistance"]
    out_ok_levels = analysis._sanitize({
        **fb,
        "advice": {**fb["advice"], "support": 9.5, "resistance": 10.5, "stop_loss": 9.5, "take_profit": 10.5},
    }, fb, price)
    assert (out_ok_levels["advice"]["support"] == 9.5), str(out_ok_levels["advice"]["support"])
    assert (out_ok_levels["advice"]["resistance"] == 10.5), str(out_ok_levels["advice"]["resistance"])
    assert (out_ok_levels["advice"]["take_profit"] == 10.5), str(out_ok_levels["advice"]["take_profit"])

    # 3.1) 价位非数字（字符串）：回退规则值
    llm_str_level = {**fb, "advice": {**fb["advice"], "support": "约九块五", "resistance": None}}
    out_str = analysis._sanitize(llm_str_level, fb, price)
    assert (out_str["advice"]["support"] == fb["advice"]["support"]), str(out_str["advice"]["support"])
    assert (out_str["advice"]["resistance"] == fb["advice"]["resistance"]), str(out_str["advice"]["resistance"])

    # 3.2) 价位 0 或负数：视为非法 → 回退
    llm_zero = {**fb, "advice": {**fb["advice"], "stop_loss": 0, "take_profit": -5}}
    out_zero = analysis._sanitize(llm_zero, fb, price)
    assert (out_zero["advice"]["stop_loss"] == fb["advice"]["stop_loss"]), str(out_zero["advice"]["stop_loss"])
    assert (out_zero["advice"]["take_profit"] == fb["advice"]["take_profit"]), str(out_zero["advice"]["take_profit"])

    # 4) confidence 越界：150 → 截到 100；-10 → 截到 0；"abc" → 默认 70
    llm_conf_high = {**fb, "advice": {**fb["advice"], "confidence": 150}}
    out_ch = analysis._sanitize(llm_conf_high, fb, price)
    assert (out_ch["advice"]["confidence"] == 100), str(out_ch["advice"]["confidence"])
    llm_conf_low = {**fb, "advice": {**fb["advice"], "confidence": -10}}
    out_cl = analysis._sanitize(llm_conf_low, fb, price)
    assert (out_cl["advice"]["confidence"] == 0), str(out_cl["advice"]["confidence"])
    llm_conf_str = {**fb, "advice": {**fb["advice"], "confidence": "abc"}}
    out_cs = analysis._sanitize(llm_conf_str, fb, price)
    assert (out_cs["advice"]["confidence"] == 70), str(out_cs["advice"]["confidence"])

    # 5) risk.opportunities/risks 非列表（字符串/空）：回退规则值；超过 5 条截断
    llm_risk_str = {**fb, "risk": {**fb["risk"], "opportunities": "多头格局", "risks": []}}
    out_rs = analysis._sanitize(llm_risk_str, fb, price)
    assert (isinstance(out_rs["risk"]["opportunities"], list)
          and out_rs["risk"]["opportunities"] == ["多头格局"]), str(out_rs["risk"]["opportunities"])
    assert (out_rs["risk"]["risks"] == fb["risk"]["risks"]), str(out_rs["risk"]["risks"])

    # 5.1) opportunities 超过 5 条：截断到 5
    llm_too_many = {**fb, "risk": {**fb["risk"], "opportunities": [f"机会{i}" for i in range(8)]}}
    out_tm = analysis._sanitize(llm_too_many, fb, price)
    assert (len(out_tm["risk"]["opportunities"]) == 5), str(out_tm["risk"]["opportunities"])

    # 6) 各 section 顶层字段缺失：用 fallback 补（不全空）
    llm_partial = {"advice": fb["advice"], "risk": {**fb["risk"]}}
    # trend/capital/fundamental 缺失，应整体用 fallback
    out_partial = analysis._sanitize(llm_partial, fb, price)
    assert (out_partial["trend"] == fb["trend"]
          and out_partial["capital"] == fb["capital"] and out_partial["fundamental"] == fb["fundamental"]), str({k: out_partial.get(k) for k in ("trend", "capital", "fundamental")})

    # 6.1) trend.summary 是空串：视为空值，用 fallback 的 summary 补
    llm_empty_summary = {**fb, "trend": {**fb["trend"], "summary": ""}}
    out_es = analysis._sanitize(llm_empty_summary, fb, price)
    assert (out_es["trend"]["summary"] == fb["trend"]["summary"]), str(out_es["trend"]["summary"])

    # 7) 低置信度撤销激进建议：confidence<50 且 action 为积极/清仓 → 撤销为持有观望
    llm_agg_low = {**fb, "advice": {**fb["advice"], "action": "积极持仓/加仓", "confidence": 40}}
    out_al = analysis._sanitize(llm_agg_low, fb, price)
    assert (out_al["advice"]["action"] == "持有观望"), str(out_al["advice"]["action"])
    assert ("action_note" in out_al["advice"] and "置信度过低" in out_al["advice"]["action_note"]), str(out_al["advice"].get("action_note"))
    llm_liq_low = {**fb, "advice": {**fb["advice"], "action": "清仓离场", "confidence": 30}}
    out_ll = analysis._sanitize(llm_liq_low, fb, price)
    assert (out_ll["advice"]["action"] == "持有观望"), str(out_ll["advice"]["action"])

    # 7.1) 非激进 action 低置信度不撤销；高置信度激进不撤销
    llm_hold_low = {**fb, "advice": {**fb["advice"], "action": "持有观望", "confidence": 40}}
    out_hl = analysis._sanitize(llm_hold_low, fb, price)
    assert (out_hl["advice"]["action"] == "持有观望"), str(out_hl["advice"]["action"])
    llm_agg_high = {**fb, "advice": {**fb["advice"], "action": "积极持仓/加仓", "confidence": 80}}
    out_ah = analysis._sanitize(llm_agg_high, fb, price)
    assert (out_ah["advice"]["action"] == "积极持仓/加仓"), str(out_ah["advice"]["action"])

    # 8) risk 列表按含数字条目优先：泛泛而谈的空话排后
    llm_risk_order = {**fb, "risk": {
        **fb["risk"],
        "opportunities": ["重大利好催化", "营收同比增长20%", "订单饱满"],
    }}
    out_ro = analysis._sanitize(llm_risk_order, fb, price)
    assert ("20%" in out_ro["risk"]["opportunities"][0]), str(out_ro["risk"]["opportunities"])





def test_payload_quality() -> None:
    # 构造 60 根 K 线：volume 前 30 根 1e8 股、后 30 根 5e7 股，均量可验证
    kline = [
        {"date": f"2026-{i // 30 + 1:02d}-{i % 30 + 1:02d}",
         "close": 9.0 + i * 0.01, "volume": 1e8 if i < 30 else 5e7}
        for i in range(60)
    ]
    detail = _mk_detail(
        quote={"code": "600000", "name": "浦发银行", "price": 9.6, "prev_close": 9.5,
               "change": 0.1, "change_pct": 1.05, "open": 9.5, "high": 9.7, "low": 9.4,
               "volume": 5e6, "amount": 4.8e8, "turnover": 1.0, "volume_ratio": 1.2},
        kline=kline,
        ma=[_ma_item(5, 9.5), _ma_item(10, 9.4), _ma_item(20, 9.3), _ma_item(60, 9.0)],
        ma_summary={"arrangement": "多头排列", "above_count": 4,
                    "above": ["MA5", "MA10", "MA20", "MA60"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 1e8, "main_last": 0.5e8,
                                          "main_last5": 0.3e8, "streak": 3, "streak_dir": "流入"}},
        support_resistance={"support": 9.4, "resistance": 9.8, "state": "突破"},
    )
    payload = analysis.build_payload(detail, None, None)

    # 1) 近 60 日均量字段存在且值正确：
    #    sum = 1e8*30 + 5e7*30 = 4.5e9 股；avg = 4.5e9/60 = 7.5e7 股
    #    万手 = 7.5e7 / 1e6 = 75.0
    base = payload["基础数据"]
    assert ("近60日均量_万手" in base), str(list(base.keys()))
    assert (base["近60日均量_万手"] is not None and abs(base["近60日均量_万手"] - 75.0) < 0.01), str(base["近60日均量_万手"])

    # 2) 近 60 日收盘序列存在且长度 60
    ma_block = payload["均线技术数据"]
    assert ("近60日收盘序列" in ma_block), str(list(ma_block.keys()))
    seq60 = ma_block["近60日收盘序列"]
    assert (len(seq60) == 60), f"len={len(seq60)}"

    # 3) 近 30 日序列仍在且长度为 30（未误删旧字段）
    assert (len(ma_block["近30日收盘序列"]) == 30), str(len(ma_block["近30日收盘序列"]))


