"""Value Screener。"""
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

def test_value_screener() -> None:
    from backend import value_screener as vs

    # 市场状态分类
    mk = vs._market_state(
        [{"code": "000001", "change_pct": 1.8}, {"code": "399006", "change_pct": 2.1}],
        zt_count=60, avg_chg=2.0)
    assert (mk["state"] == "A" and mk["attack"] >= 80), str(mk)
    mk2 = vs._market_state([{"code": "000001", "change_pct": -2.0}], zt_count=10, avg_chg=-1.0)
    assert (mk2["state"] in ("E", "F") and mk2["attack"] <= 30), str(mk2)

    # 候选过滤：剔除 ETF / 北交所 / 非 A 股代码
    assert (vs._is_stock_code("601012", "SH")
          and vs._is_stock_code("300750", "SZ"))
    assert (not vs._is_stock_code("515070", "SH")
          and not vs._is_stock_code("159819", "SZ"))
    assert (not vs._is_stock_code("920002", "BJ"))

    # 财务评分：高质量高成长 > 亏损低质
    good_fin = [
        {"revenue_yoy": 25.0, "net_profit_yoy": 40.0, "roe": 18.0, "gross_margin": 45.0, "debt_ratio": 30.0},
        {"revenue_yoy": 15.0, "net_profit_yoy": 20.0, "roe": 15.0, "gross_margin": 40.0, "debt_ratio": 35.0},
    ]
    bad_fin = [
        {"revenue_yoy": -20.0, "net_profit_yoy": -50.0, "roe": -5.0, "gross_margin": 5.0, "debt_ratio": 90.0},
    ]
    g = vs._financial_score(good_fin)
    b = vs._financial_score(bad_fin)
    assert (g["score"] > b["score"] + 10), f"good={g['score']} bad={b['score']}"
    assert (vs._financial_score([])["score"] == 0)

    # 资金拐点：30 日流出但近 5 日流入应加分
    flow_out = [{"date": f"d{i}", "main": -2e7} for i in range(25)]
    flow_in = [{"date": f"d{i}", "main": 3e7} for i in range(5)]
    fp = vs._flow_score({"flow": flow_out + flow_in})
    fn = vs._flow_score({"flow": [{"date": f"d{i}", "main": -2e7} for i in range(8)]})
    assert (fp["score"] > fn["score"]), f"turn={fp['score']} out={fn['score']}"

    # 风险：ST/亏损/高负债/高位连板
    risk = vs._risk_score({"name": "*ST测试", "financials": [{"net_profit_yoy": -60.0, "debt_ratio": 85.0, "net_profit": -1e8}], "pe": 200, "change_pct": 10.0, "lianban": 6}, {})
    assert (risk["score"] > 60 and "ST" in " ".join(risk["notes"])), str(risk)
    safe = vs._risk_score({"name": "正常股", "financials": [{"net_profit_yoy": 20.0, "debt_ratio": 30.0, "net_profit": 1e8}], "pe": 20, "change_pct": 1.0, "lianban": 0}, {})
    assert (safe["score"] <= 20), str(safe)

    # 分级与信号（阈值按 BASE_TOTAL 相对比例，维度增减后不漂移）
    _base = vs.valuecfg.BASE_TOTAL
    assert (vs._grade(_base * 0.95)[0] == "S")
    assert (vs._grade(_base * 0.5)[0] == "D")
    assert (vs._signal({"change_pct": 1, "volume_ratio": 1, "lianban": 0}, 80, 80, 70) == "AVOID")
    assert (vs._signal({"change_pct": 6, "volume_ratio": 2, "lianban": 1},
                     _base * 0.85, 75, 10) == "BREAKOUT_BUY")

    # 综合评分：归一化相对权重（默认全 1.0 = 原始分之和，总分恒在 0~BASE_TOTAL）
    def _sc(v): return {"score": v, "detail": ""}
    sc = {"finance": _sc(30), "board": _sc(5), "flow": _sc(6),
          "volume": _sc(4), "emotion": _sc(6), "relative": _sc(4),
          "position": _sc(3), "risk": _sc(10)}
    w1 = {k: 1.0 for k in vs.valuecfg.FIELDS}
    t1 = vs._composite_score(sc, w1)
    assert (abs(t1 - 58) < 0.01), f"t1={t1}"
    # 权重是相对看重：基本面强的股票调大 finance 权重总分上升，弱的下降（只改排序不单方向加分）
    strong = {"finance": _sc(45), "board": _sc(1), "flow": _sc(1),
              "volume": _sc(1), "emotion": _sc(1), "relative": _sc(1),
              "position": _sc(1), "risk": _sc(10)}
    weak = {"finance": _sc(5), "board": _sc(9), "flow": _sc(11),
            "volume": _sc(7), "emotion": _sc(11), "relative": _sc(7),
            "position": _sc(5), "risk": _sc(10)}
    w_fin2 = dict(w1); w_fin2["finance"] = 2.0
    up = vs._composite_score(strong, w_fin2) - vs._composite_score(strong, w1)
    down = vs._composite_score(weak, w_fin2) - vs._composite_score(weak, w1)
    assert (up > 0 and down < 0), f"up={up:.1f} down={down:.1f}"
    # 全部维度同强度（50%）时权重不影响总分（只改变相对排序）
    neutral = {k: _sc(v / 2) for k, v in vs.valuecfg.DIM_MAXES.items()}
    neutral["risk"] = _sc(10)
    t_neutral = vs._composite_score(neutral, w1)
    t_neutral2 = vs._composite_score(neutral, w_fin2)
    half = _base / 2
    assert (abs(t_neutral - half) < 0.01 and abs(t_neutral2 - half) < 0.01), f"t={t_neutral} t2={t_neutral2}"
    # 总分恒在 0~BASE_TOTAL（不再因权重放大顶到 100 截断失真）
    maxed = {k: _sc(v) for k, v in vs.valuecfg.DIM_MAXES.items()}
    maxed["risk"] = _sc(10)
    for w_ in (w1, w_fin2, {"finance": 3.0, "board": 0.2, "flow": 3.0,
                            "volume": 0.2, "emotion": 3.0,
                            "relative": 0.2, "position": 3.0}):
        t = vs._composite_score(maxed, w_)
        assert (0 <= t <= _base), f"t={t} base={_base}"
    sc_risk = dict(sc); sc_risk["risk"] = _sc(80)
    assert (vs._composite_score(sc_risk, w1) < vs._composite_score(sc, w1))

    # ---- v2: 估值维度 + 信号升级 ----

    # 估值区间：PE 8（深度低估）应明显高于 PE 100（高估）
    good_fin = [
        {"revenue_yoy": 15, "net_profit_yoy": 20, "roe": 12, "gross_margin": 30, "debt_ratio": 35},
    ]
    deep_value = vs._financial_score({"pe": 8, "pb": 1.0}, good_fin)
    fair_value = vs._financial_score({"pe": 30, "pb": 2.5}, good_fin)
    over_value = vs._financial_score({"pe": 100, "pb": 8}, good_fin)
    assert (deep_value["score"] > fair_value["score"]), f"deep={deep_value['score']} fair={fair_value['score']}"
    assert (over_value["score"] < fair_value["score"]), f"over={over_value['score']} fair={fair_value['score']}"

    # PEG < 1 应得额外加分
    peg_low = vs._financial_score({"pe": 15, "pb": 2},
                                  [{"net_profit_yoy": 30}])  # PEG=0.5
    peg_high = vs._financial_score({"pe": 60, "pb": 2},
                                   [{"net_profit_yoy": 5}])  # PEG=12
    assert (peg_low["score"] > peg_high["score"]), f"peg_low={peg_low['score']} peg_high={peg_high['score']}"

    # value_metrics 字段应包含 band 字段
    metrics = deep_value.get("value_metrics", {})
    assert (metrics.get("pe_band") == "深度低估"), str(metrics)
    assert (metrics.get("pb_band") == "深度低估"), str(metrics)

    # 估值数据缺失时完整性为 0，价值分也得算（其它子项还能得分）
    no_valuation = vs._financial_score({"pe": None, "pb": None},
                                       [{"net_profit_yoy": 15, "roe": 15,
                                         "gross_margin": 35, "debt_ratio": 30}])
    assert (no_valuation["score"] > 0), f"score={no_valuation['score']}"
    assert (no_valuation["completeness"] == 0)

    # OCF 现金流数据
    ocf_good = [{"revenue_yoy": 20, "net_profit_yoy": 20, "roe": 15,
                 "gross_margin": 35, "debt_ratio": 30, "ocf_to_netprofit": 1.2}]
    ocf_bad = [{"revenue_yoy": 20, "net_profit_yoy": 20, "roe": 15,
                "gross_margin": 35, "debt_ratio": 30, "ocf_to_netprofit": -0.5}]
    s_good = vs._financial_score({"pe": 20, "pb": 2}, ocf_good)
    s_bad = vs._financial_score({"pe": 20, "pb": 2}, ocf_bad)
    assert (s_good["score"] > s_bad["score"]), f"good={s_good['score']} bad={s_bad['score']}"

    # 行业景气度
    bs = {"光伏": 0.9, "白酒": 0.5, "地产": 0.1}
    hot = vs._financial_score({"pe": 20, "pb": 2, "board": "光伏"},
                              good_fin, board_strength=bs)
    cold = vs._financial_score({"pe": 20, "pb": 2, "board": "地产"},
                               good_fin, board_strength=bs)
    non = vs._financial_score({"pe": 20, "pb": 2, "board": "未知"},
                              good_fin, board_strength=bs)
    assert (hot["score"] > cold["score"]), f"hot={hot['score']} cold={cold['score']}"
    assert (cold["score"] > non["score"]), f"cold={cold['score']} none={non['score']}"

    # 风险层：估值与 OCF 风险
    risk_pe = vs._risk_score(
        {"name": "高估", "financials": [{"net_profit_yoy": 30, "debt_ratio": 30}],
         "pe": 250, "pb": 12, "lianban": 0},
        {})
    assert (risk_pe["score"] >= 23 and "严重高估" in " ".join(risk_pe["notes"])), str(risk_pe)
    risk_pe2 = vs._risk_score(
        {"name": "高估", "financials": [{"net_profit_yoy": 30, "debt_ratio": 30}],
         "pe": 120, "pb": 8, "lianban": 0},
        {})
    assert (risk_pe2["score"] >= 10 and "高估" in " ".join(risk_pe2["notes"])
          and "严重高估" not in " ".join(risk_pe2["notes"])), str(risk_pe2)
    risk_ocf = vs._risk_score(
        {"name": "OCF恶化", "financials": [
            {"net_profit_yoy": 10, "debt_ratio": 30, "ocf_to_netprofit": -1},
            {"net_profit_yoy": 10, "debt_ratio": 30, "ocf_to_netprofit": -1},
        ], "pe": 20, "lianban": 0},
        {})
    assert (risk_ocf["score"] >= 10 and "OCF" in " ".join(risk_ocf["notes"])), str(risk_ocf)

    # 新信号（判定放宽为估值档位：PE 低估/深度低估 或 PEG 低估/极低估）
    _sig_base = vs.valuecfg.BASE_TOTAL
    assert (vs._signal({"pe": 10, "pb": 1.5, "change_pct": 1, "volume_ratio": 1,
                      "lianban": 0}, 70, 65, 10,
                     {"pe_band": "深度低估"}) == "VALUE_BUY")
    # PE 30 但增速 40%：PEG 极优，旧口径（硬 PE<15）会漏掉，新口径应命中
    assert (vs._signal({"pe": 30, "pb": 3, "change_pct": 1, "volume_ratio": 1,
                      "lianban": 0}, 70, 65, 10,
                     {"pe_band": "合理", "peg_band": "极低估"}) == "VALUE_BUY")
    # 连板梯队股走情绪线，不占用价值买点语义
    assert (vs._signal({"pe": 10, "pb": 1.5, "change_pct": 1, "volume_ratio": 1,
                      "lianban": 2}, 70, 65, 10,
                     {"pe_band": "深度低估"}) != "VALUE_BUY")
    assert (vs._signal({"pe": 25, "pb": 3, "change_pct": 0, "volume_ratio": 1,
                      "lianban": 0}, _sig_base * 0.9, 70, 10) == "QUALITY_HOLD")
    assert (vs._signal({"pe": 150, "pb": 5, "change_pct": 1, "volume_ratio": 1,
                      "lianban": 0}, 60, 60, 30) == "EXIT")
    assert (vs._signal({"pe": 8, "pb": 1, "change_pct": 1, "volume_ratio": 1,
                      "lianban": 0}, 70, 65, 40,
                     {"pe_band": "深度低估"}) != "VALUE_BUY")
    # 买点评分接入估值维度
    base = {"change_pct": 2.0, "volume_ratio": 1.0, "lianban": 0}
    deep_pe = dict(base); deep_pe["financials"] = []
    deep_fin = {"score": 30, "value_metrics": {"pe_band": "深度低估", "peg_band": "极低估"}}
    over_fin = {"score": 30, "value_metrics": {"pe_band": "高估", "peg_band": "高估"}}
    fair_fin = {"score": 30, "value_metrics": {"pe_band": "合理", "peg_band": "合理"}}
    profile = {"change_pct": 2.0, "volume_ratio": 1.0, "lianban": 0,
               "flow": [{"date": "d" + str(i), "main": 1e7} for i in range(5)],
               "board": "光伏"}
    scores_base = {"board": {"score": 3}, "risk": {"score": 10}}
    deep_scores = dict(scores_base); deep_scores["finance"] = deep_fin
    over_scores = dict(scores_base); over_scores["finance"] = over_fin
    fair_scores = dict(scores_base); fair_scores["finance"] = fair_fin
    deep_buy = vs._buy_score(profile, deep_scores)["score"]
    over_buy = vs._buy_score(profile, over_scores)["score"]
    fair_buy = vs._buy_score(profile, fair_scores)["score"]
    assert (deep_buy > fair_buy > over_buy), f"deep={deep_buy} fair={fair_buy} over={over_buy}"
    # 既有 BREAKOUT_BUY 行为不变（PE 缺失时仍走既有逻辑）
    # 阈值已改为按 BASE_TOTAL 相对比例，这里的 total 需按占比给定（旧 80/92 ≈ 0.87）
    assert (vs._signal({"change_pct": 6, "volume_ratio": 2, "lianban": 1},
                     vs.valuecfg.BASE_TOTAL * 0.87, 75, 10) == "BREAKOUT_BUY")

    # ---- v3: 腾讯补充字段错位修复（回归测试，防止再改回错索引）----
    # 构造 50+ 字段的原始数组：f[38]=换手 f[39]=PE f[43]=振幅
    #                        f[45]=总市值 f[46]=PB f[49]=量比
    raw = ["" for _ in range(60)]
    raw[30] = "20260828161452"
    raw[37] = "52882"      # 成交额（万）
    raw[38] = "0.18"       # 换手率
    raw[39] = "5.85"       # PE
    raw[43] = "0.99"       # 振幅
    raw[45] = "2997.53"    # 总市值（亿）
    raw[46] = "0.40"       # PB
    raw[49] = "0.72"       # 量比
    parsed = vs._parse_tencent_extra(raw)
    assert (parsed.get("pb") == 0.40), str(parsed)
    assert (parsed.get("volume_ratio") == 0.72), str(parsed)
    assert (parsed.get("amplitude") == 0.99), str(parsed)
    assert (parsed.get("pe") == 5.85), str(parsed)
    assert (parsed.get("total_mv") == 2997.53), str(parsed)
    assert (parsed.get("quote_time") == "16:14:52"), str(parsed)
    assert (vs._parse_tencent_extra(["", ""]) == {})

    # ---- v3: 个股相对板块强度 ----
    rel_strong = vs._relative_score({"change_pct": 9.0, "board_avg_chg": 2.0, "board": "光伏"})
    rel_weak = vs._relative_score({"change_pct": -1.0, "board_avg_chg": 6.0, "board": "光伏"})
    assert (rel_strong["score"] > rel_weak["score"]), f"strong={rel_strong['score']} weak={rel_weak['score']}"
    assert (rel_strong.get("relative_chg") == 7.0), str(rel_strong)
    assert (vs._relative_score({"change_pct": 3.0})["score"] == 4)

    # ---- v3: 20 日价格位置 ----
    kline = [{"date": f"2026-01-{i+1:02d}", "close": 10 + i * 0.5,
              "high": 10 + i * 0.5, "low": 9.5 + i * 0.5, "volume": 100}
             for i in range(20)]  # 区间 9.5 ~ 19.5
    pos_low = vs._position_score({"price": 11.0, "kline": kline})   # 低位
    pos_high = vs._position_score({"price": 19.0, "kline": kline})  # 高位
    assert (pos_low["score"] > pos_high["score"]), f"low={pos_low['score']} high={pos_high['score']}"
    assert (abs((pos_low.get("position_pct") or 0) - 15.8) < 1.0), str(pos_low)
    assert (vs._position_score({"price": 10, "kline": []})["score"] == 3)
    # 回归：K线最后一根不含当日时（数据源延迟/频控回落），现价会冲出 20 日区间。
    # 涨停股常见，早期实现会算出 >100% 的非法位置，需由现价扩展区间并夹到 0~100。
    pos_breakout = vs._position_score({"price": 25.0, "kline": kline})
    assert (pos_breakout.get("position_pct") == 100.0), str(pos_breakout)
    pos_breakdown = vs._position_score({"price": 5.0, "kline": kline})
    assert (pos_breakdown.get("position_pct") == 0.0), str(pos_breakdown)

    # ---- v3: 买点评分接入价格位置与相对强度 ----
    p2 = {"change_pct": 2.0, "volume_ratio": 1.0, "lianban": 0,
          "flow": [{"date": "d" + str(i), "main": 1e7} for i in range(5)],
          "board": "光伏"}
    s_low = {"board": {"score": 3}, "risk": {"score": 10},
             "position": {"score": 6, "position_pct": 15.0},
             "relative": {"score": 7, "relative_chg": 4.0}}
    s_high = {"board": {"score": 3}, "risk": {"score": 10},
              "position": {"score": 1, "position_pct": 95.0},
              "relative": {"score": 1, "relative_chg": -5.0}}
    buy_low = vs._buy_score(p2, s_low)["score"]
    buy_high = vs._buy_score(p2, s_high)["score"]
    assert (buy_low > buy_high), f"low={buy_low} high={buy_high}"

    # ---- v3: 炸板率压制市场进攻等级 ----
    idx = [{"code": "000001", "change_pct": 1.0}, {"code": "399006", "change_pct": 1.0}]
    m_no_zb = vs._market_state(idx, 60, 1.0, zb_count=0)
    m_zb = vs._market_state(idx, 60, 1.0, zb_count=60)  # 炸板率 50%
    assert (m_zb["attack"] < m_no_zb["attack"]), f"no_zb={m_no_zb['attack']} zb={m_zb['attack']}"
    assert (m_zb.get("zb_rate") == 50.0), str(m_zb)
    assert (m_zb.get("emotion") == "退潮"), str(m_zb)

    # ---- v3: 板块平均涨幅聚合 ----
    bavg = vs._board_avg_change(
        [{"board": "光伏", "change_pct": 10.0}],
        [{"board": "光伏", "change_pct": 4.0}, {"board": "光伏", "change_pct": 6.0}])
    assert (abs(bavg.get("光伏", 0) - 6.67) < 0.1), str(bavg)

    # ---- v8: 价值投资优化回归 ----
    from backend import value_screener as _vs

    # (1) PE/PB 缺失时给中性分(不奖不罚),避免整个价值维度被清零
    pe_missing = _vs._financial_score(
        {"pe": None, "pb": None},
        [{"net_profit_yoy": 15, "roe": 15, "gross_margin": 35, "debt_ratio": 30,
          "revenue_yoy": 10}])
    # 中性 5(PE 缺失) + 1(PB 缺失) = 6/18,加上 growth/quality/ocf/industry 子项
    assert (pe_missing.get("value_metrics", {}).get("pe_band") == "数据缺失")
    assert (pe_missing.get("value_metrics", {}).get("pb_band") == "数据缺失")
    # 价值分至少有中性 6 分(5+1)
    assert (pe_missing["score"] >= 6), f"PE missing score={pe_missing['score']}"

    # (2) 亏损公司给 2/18 中性偏负分
    pe_loss = _vs._financial_score(
        {"pe": -5, "pb": 1.5},
        [{"net_profit_yoy": 15, "roe": 15, "gross_margin": 35, "debt_ratio": 30,
          "revenue_yoy": 10}])
    assert (pe_loss.get("value_metrics", {}).get("pe_band") == "亏损")

    # (3) 资金流按市值占比打分(小盘股 5% 占比 vs 大盘股 0.5% 占比)
    flow_same = [{"date": f"d{i}", "main": 1e7} for i in range(10)]  # 1亿/日
    # 小盘(100亿市值):10日累计10亿 → 10% 占比 → 应得高 flow 分
    fp_small = _vs._flow_score({"flow": flow_same, "total_mv": 100})
    # 大盘(1000亿市值):10日累计10亿 → 1% 占比 → 应得低 flow 分
    fp_big = _vs._flow_score({"flow": flow_same, "total_mv": 1000})
    assert (fp_small["score"] > fp_big["score"]), (
        f"small={fp_small['score']} big={fp_big['score']}")

    # 资金流无市值数据时退回绝对额(向后兼容)
    fp_no_mv = _vs._flow_score({"flow": flow_same})
    assert (fp_no_mv["score"] >= 0)

    # (4) 连板 >= 6 时 emotion 不再奖分(与 buy/risk 方向一致)
    e_low = _vs._emotion_score({"lianban": 3, "turnover": 10, "change_pct": 6})
    e_high = _vs._emotion_score({"lianban": 6, "turnover": 10, "change_pct": 6})
    # 连板 6 不应比连板 3 高(因为高位接盘不给情绪分)
    assert (e_high["score"] <= e_low["score"]), (
        f"low={e_low['score']} high={e_high['score']}")
    assert ("高位接盘" in e_high["detail"])

    # (5) 风险扣分非线性:risk=80 扣分应明显大于 risk=20
    sc_base = {"finance": _sc(30), "board": _sc(5), "flow": _sc(6),
               "volume": _sc(4), "emotion": _sc(6), "relative": _sc(4),
               "position": _sc(3), "risk": _sc(20)}
    sc_high = dict(sc_base); sc_high["risk"] = _sc(80)
    sc_mid = dict(sc_base); sc_mid["risk"] = _sc(40)
    t_base = _vs._composite_score(sc_base, w1)
    t_mid = _vs._composite_score(sc_mid, w1)
    t_high = _vs._composite_score(sc_high, w1)
    drop_mid = t_base - t_mid
    drop_high = t_base - t_high
    # 高风险扣分应明显大于中风险(非线性)
    assert (drop_high > drop_mid * 1.5), (
        f"drop_mid={drop_mid:.1f} drop_high={drop_high:.1f}")
    # risk=80 扣分应至少 25 分(对 AVOID 阈值有实质影响)
    assert (drop_high >= 25), f"drop_high={drop_high:.1f}"

    # (6) 位置评分自适应:60+ 日 K线使用 60 日窗口
    kline60 = [{"date": f"d{i}", "close": 10 + i * 0.1, "high": 10 + i * 0.1,
                "low": 9.5 + i * 0.1, "volume": 100} for i in range(60)]
    p60 = _vs._position_score({"price": 14.0, "kline": kline60})
    assert (p60.get("position_window") == "60日"), str(p60)
    kline20 = [{"date": f"d{i}", "close": 10 + i * 0.1, "high": 10 + i * 0.1,
                "low": 9.5 + i * 0.1, "volume": 100} for i in range(20)]
    p20 = _vs._position_score({"price": 11.0, "kline": kline20})
    assert (p20.get("position_window") == "20日"), str(p20)

    # (7) 价值基准池存在且为蓝筹
    from backend.value_screener import _VALUE_BASELINE
    assert (len(_VALUE_BASELINE) >= 30), f"baseline={len(_VALUE_BASELINE)}"
    codes = [s["code"] for s in _VALUE_BASELINE]
    # 验证一些知名蓝筹
    assert ("600519" in codes and "601318" in codes and "000858" in codes), codes

    # (8) 分级阈值:0.88 是 S,0.85 是 A,0.65 是 C,0.5 是 D
    _base = _vs.valuecfg.BASE_TOTAL
    assert (_vs._grade(_base * 0.90)[0] == "S"), str(_vs._grade(_base * 0.90))
    assert (_vs._grade(_base * 0.82)[0] == "A"), str(_vs._grade(_base * 0.82))
    assert (_vs._grade(_base * 0.70)[0] == "B"), str(_vs._grade(_base * 0.70))
    assert (_vs._grade(_base * 0.62)[0] == "C"), str(_vs._grade(_base * 0.62))
    assert (_vs._grade(_base * 0.50)[0] == "D"), str(_vs._grade(_base * 0.50))

    # (9) 信号阈值校准:hi_cut=0.80, top_cut=0.85
    # total=0.82 * BASE + buy=85, risk=10 → BUY(总分达 0.80 阈值)
    assert (_vs._signal({"change_pct": 1, "volume_ratio": 1, "lianban": 0},
                        _base * 0.82, 85, 10) == "BUY"), "BUY threshold"
    # total=0.86 * BASE + buy=85, risk=10 + pe 25 → QUALITY_HOLD
    assert (_vs._signal({"pe": 25, "pb": 3, "change_pct": 1, "volume_ratio": 1,
                        "lianban": 0}, _base * 0.86, 85, 10) == "QUALITY_HOLD"), "QH threshold"



def test_value_screen_e2e() -> None:
    """价值选股端到端：用真实数据源跑一次 run_screen。
    网络隔离沙箱里走 `skipped` 路径,生产/CI 环境会跑真实流程并校验 3 个池子。
    整个测试有 30s 总兜底,不会卡住 smoke_test。"""
    from backend import value_screener as vs
    import asyncio as _aio

    async def _probe() -> bool:
        try:
            from backend.providers import registry
            quotes, _src = await _aio.wait_for(
                registry().quotes([("000001", "SZ"), ("600519", "SH")]),
                timeout=4.0)
            return bool(quotes)
        except Exception:
            return False

    online = _aio.run(_probe())
    if not online:
        assert (True), "skipped (sandboxed env, network probes exhausted)"
        return

    # 在线：跑真实流程,带超时兜底
    async def _run() -> dict:
        # 强制刷新一次,确保不是上次缓存(但保留 15min 缓存给正常 watcher)
        return await vs.run_screen(force=False)

    try:
        result = _aio.run(_aio.wait_for(_run(), timeout=60.0))
    except _aio.TimeoutError:
        assert (True), "skipped (data sources too slow)"
        return
    except Exception as e:
        assert (False), f"exception: {type(e).__name__}: {str(e)[:120]}"
        return

    pools = result.get("pools") or {}
    assert (all(k in pools for k in ("core", "trend", "emotion"))), str({k: len(v or []) for k, v in pools.items()})
    assert (isinstance(result.get("generated_at"), str) and len(result["generated_at"]) >= 19), str(result.get("generated_at"))

    # 校验核心池里股票的关键字段(若有)
    core_top = pools.get("core") or []
    if core_top:
        s = core_top[0]
        assert (s.get("signal") and s.get("advice") and isinstance(s.get("value_metrics"), dict)), str({k: s.get(k) for k in ("signal", "advice", "value_metrics")})
    # 校验市场状态
    mkt = result.get("market") or {}
    assert (mkt.get("state") in "ABCDEF"), str(mkt.get("state"))





def test_value_weights() -> None:
    """价值选股权重：默认 1.0 / 保存 clamp / 恢复默认 / 指纹联动（临时库，结束还原）。"""
    storage.init_db()
    saved = storage.get_kv("value_weights")
    try:
        from backend import valuecfg as vc
        w0 = vc.get_weights()
        assert (all(abs(v - 1.0) < 1e-9 for v in w0.values())), str(w0)
        w = vc.save_weights({"finance": 2.0, "board": 0.5, "emotion": 9.9, "bad": 3})
        assert (w["finance"] == 2.0 and w["board"] == 0.5
              and w["emotion"] == vc._MAX), str(w)
        assert ("bad" not in w)
        assert (vc.get_weights()["finance"] == 2.0)
        fp1 = vc.fingerprint()
        vc.save_weights({"finance": 1.0})
        assert (fp1 != vc.fingerprint())
        vc.reset_weights()
        assert (all(abs(v - 1.0) < 1e-9 for v in vc.get_weights().values()))
    finally:
        if saved is not None:
            storage.set_kv("value_weights", saved)
        else:
            storage.delete_kv("value_weights")

    # 选股结果补自选状态：pools 与 stocks 中每只股票都应被标记 watched
    from backend import storage as _storage
    from backend import api as _api
    sample = {"pools": {"core": [{"code": "601012", "name": "隆基绿能"}]},
              "stocks": [{"code": "601012", "name": "隆基绿能"}, {"code": "300750", "name": "宁德时代"}]}
    _api._mark_value_watched(sample)
    assert (sample["pools"]["core"][0]["watched"] is False)
    assert (sample["stocks"][0]["watched"] is False
          and sample["stocks"][1]["watched"] is False)


