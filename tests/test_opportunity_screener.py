"""机会投资筛选器 V8.0 纯函数测试（不发网络请求）。"""
from __future__ import annotations

from tests._common import *  # noqa: F401,F403  公共导入见 tests/_common.py
from backend import opportunity_screener as ops


def _zt(count: int, rows: list[dict]) -> dict:
    return {"count": count, "rows": rows}


def _mk_emotion(zt_c=100, zb_c=5, dt_c=0, idx=1.0, hot=5.0):
    zt_rows = [{"code": f"30000{i}", "name": f"股{i}", "market": "SZ",
                "board": "AI", "lianban": 7, "seal_amount": 2e8,
                "change_pct": 9.95, "turnover": 10} for i in range(5)]
    return ops._market_emotion(
        _zt(zt_c, zt_rows), {"count": zb_c, "rows": [{"hybk": "AI"}]},
        {"count": dt_c, "rows": []},
        [{"code": "000001", "change_pct": idx}],
        [{"board": "AI", "change_pct": hot}])


def test_market_emotion_tiers() -> None:
    # A：满配强环境（涨停100/连板7/炸板率低/无跌停/20cm活跃/热点均涨5%）
    m = _mk_emotion()
    assert m["emotion"] == "A" and m["emotion_score"] >= 90, str(m)

    # D 硬触发①：跌停≥30
    m2 = _mk_emotion(zt_c=100, dt_c=35)
    assert m2["emotion"] == "D" and m2["position"] == "空仓", str(m2)

    # D 硬触发②：炸板率≥0.40（6涨停4炸板）
    m3 = _mk_emotion(zt_c=6, zb_c=4)
    assert m3["emotion"] == "D", str(m3)

    # 缺源标注：跌停池缺失进 missing，不编造（zt=50 时炸板率可算=0.0，非缺失）
    m4 = ops._market_emotion(
        _zt(50, []), {"count": 0, "rows": []},
        {"count": -1, "rows": [], "error": "boom"},
        [{"code": "000001", "change_pct": 0.5}], [])
    assert "跌停池" in m4["missing"], str(m4)
    assert m4["dt_count"] is None and m4["zb_rate"] == 0.0, str(m4)

    # 炸板率真缺失：涨停与炸板计数都不可得
    m5 = ops._market_emotion(
        {"count": 0, "rows": []}, {"count": 0, "rows": []},
        {"count": 0, "rows": []},
        [{"code": "000001", "change_pct": 0.5}], [])
    assert m5["zb_rate"] is None, str(m5)


def test_board_stats_stages() -> None:
    zt_rows = [{"code": "600001", "market": "SH", "board": "AI",
                "lianban": lb, "seal_amount": 2e8 if lb == 3 else 1e7,
                "change_pct": 9.9}
               for lb in (3, 1, 1, 1, 1)]
    bstats = ops._board_stats(
        zt_rows, [{"hybk": "AI"}],
        [{"board": "AI", "change_pct": 4.0}], index_chg=1.0,
        board_flow={"AI": {"name": "AI", "chg": 4.0, "main_today": 1.2e9,
                           "main_5d": 3e9, "rank": 2}})
    ai = bstats["AI"]
    # 5 涨停 + 最高 3 连板 + 炸板 1 → 发酵期，评分 85
    assert ai["stage"] == "发酵" and ai["stage_score"] == 85, str(ai)
    assert ai["is_ferment"] is True and ai["has_leader"] is True, str(ai)
    # 资金流已接入：仅消息/产业催化恒缺；资金字段带出（亿元口径）
    assert ai["missing"] == ["消息/产业催化"], str(ai)
    assert ai["fund_today"] == 12.0 and ai["fund_5d"] == 30.0, str(ai)
    assert ai["fund_rank"] == 2 and ai["board_chg"] == 4.0, str(ai)
    assert ai["relative_strength"] == 3.0, str(ai)
    # 今日主力≥10亿(20分)+五日双正(10分) → 分母 90 下评分显著抬升
    assert ai["score"] >= 70, ai["score"]

    # 资金流缺失的板块：资金两项如实标【数据缺失】，不编造
    b_no_flow = ops._board_stats(zt_rows, [{"hybk": "AI"}],
                                 [{"board": "AI", "change_pct": 4.0}], 1.0, {})
    miss = b_no_flow["AI"]["missing"]
    assert "板块资金流入" in miss and "连续资金流入(板块级)" in miss, str(miss)

    # 退潮：无涨停且板块跌幅 < -1%（flow chg 兜底口径；板块经热门榜纳入）
    b2 = ops._board_stats([], [],
                          [{"board": "银行", "change_pct": -1.5}], index_chg=0.0,
                          board_flow={"银行": {"name": "银行", "chg": -2.0,
                                               "main_today": -5e8, "main_5d": -1e9,
                                               "rank": 80}})
    assert b2["银行"]["stage"] == "退潮" and b2["银行"]["stage_score"] == 30, str(b2)

    # 资金流强势板块（前20名且今日主力>0）无涨停也纳入展示
    b3 = ops._board_stats([], [], [], index_chg=0.0,
                          board_flow={"传媒": {"name": "传媒", "chg": 2.3,
                                               "main_today": 6.2e9, "main_5d": 6.5e9,
                                               "rank": 1},
                                      "冷门板块": {"name": "冷门板块", "chg": 0.5,
                                                   "main_today": 1e7, "main_5d": -2e8,
                                                   "rank": 200}})
    assert "传媒" in b3, list(b3)
    assert "冷门板块" not in b3, list(b3)
    assert b3["传媒"]["zt_count"] == 0, str(b3["传媒"])


def test_candidate_base_dedup_and_filter() -> None:
    from backend import opportunity_screener as ops

    zt = _zt(2, [
        {"code": "600001", "market": "SH", "name": "甲", "board": "AI",
         "lianban": 2, "change_pct": 9.9},
        {"code": "515070", "market": "SH", "name": "ETF", "board": "",
         "lianban": 0, "change_pct": 5.0},  # ETF 剔除
    ])
    hot = [{"code": "600001", "market": "SH", "name": "甲", "board": "AI",
            "change_pct": 9.9},
           {"code": "000002", "market": "SZ", "name": "乙", "board": "机器人",
            "change_pct": 5.5}]
    cands = ops._candidate_base(zt, hot)
    keys = {f"{c['code']}.{c['market']}" for c in cands}
    assert keys == {"600001.SH", "000002.SZ"}, str(cands)
    assert all(c["code"] != "515070" for c in cands)


def test_count_zt_thresholds() -> None:
    from backend import opportunity_screener as ops

    bars = ([{"code": "600000", "change_pct": 9.9}] * 3
            + [{"code": "600000", "change_pct": 1.0}]
            + [{"code": "600000", "change_pct": 9.85}])
    zt, lb = ops._count_zt(bars, 120)
    assert zt == 4 and lb == 3, (zt, lb)  # 9.85 ≥ 主板 9.8 阈值
    # 20cm 板：9.9 不算涨停
    bars20 = [{"code": "300001", "change_pct": 9.9}, {"code": "300001", "change_pct": 19.9}]
    zt2, _ = ops._count_zt(bars20, 60)
    assert zt2 == 1, zt2


def test_yaogu_score() -> None:
    from backend import opportunity_screener as ops

    bars = ([{"code": "600000", "change_pct": 9.9}] * 5
            + [{"code": "600000", "change_pct": 1.0}] * 55)
    profile = {"float_mv": 80, "price": 10.0, "turnover": 10.0,
               "main_net_inflow": 6e7, "main_net_pct": 12.0, "lianban": 0}
    bstats = {"hot_rank": 0, "max_lianban": 0}
    y = ops._yaogu_score(profile, bstats, bars)
    assert len(y["items"]) == 10 and y["missing"] == [], str(y)
    by_name = {i["name"]: i for i in y["items"]}
    assert by_name["市值弹性"]["got"] == 10, str(by_name["市值弹性"])
    assert by_name["历史爆发能力"]["got"] == 10, str(by_name["历史爆发能力"])
    assert 70 <= y["score"] <= 90, y["score"]

    # 缺市值 → 标【数据缺失】不编造
    y2 = ops._yaogu_score({k: v for k, v in profile.items() if k != "float_mv"},
                          bstats, bars)
    note2 = {i["name"]: i["note"] for i in y2["items"]}["市值弹性"]
    assert "【数据缺失】" in note2, note2


def test_fund_score_fields_and_discount() -> None:
    from backend import opportunity_screener as ops

    flow = ([{"date": f"d{i}", "main": 1e7, "xl": 5e6, "lg": 3e6} for i in range(5)]
            + [{"date": f"d{i}", "main": -2e7, "xl": -1e7, "lg": -5e6} for i in range(4)]
            + [{"date": "d9", "main": 5e7, "xl": 6e7, "lg": 2e7, "main_pct": 12}])
    bars = [{"date": f"d{i}", "amount": 2e8, "change_pct": 5.0} for i in range(6)]
    profile = {"main_net_inflow": 5e7, "main_net_pct": 12.0, "flow": flow,
               "amount": 4e8, "turnover": 10.0}
    f = ops._fund_score(profile, bars, board_cand_count=2)
    # 契约字段：day3/day5/day30/xl_today/lg_today
    assert f["day"] == 5e7 and f["day3"] == 1e7, str(f)
    assert f["day5"] == -3e7 and f["day30"] == 2e7, str(f)
    assert f["xl_today"] == 6e7 and f["lg_today"] == 2e7, str(f)
    assert f["missing"] == [], str(f)
    discounted = f["score"]
    # 同结构但近5日净流入 → 不打折，分数更高
    flow_ok = [{"date": f"d{i}", "main": 1e7, "xl": 5e6, "lg": 3e6} for i in range(9)]
    flow_ok.append({"date": "d9", "main": 5e7, "xl": 6e7, "lg": 2e7})
    f2 = ops._fund_score({**profile, "flow": flow_ok}, bars, 2)
    assert f2["score"] > discounted, (f2["score"], discounted)

    # 无 flow → 趋势项标【数据缺失】
    f3 = ops._fund_score({"main_net_inflow": 5e7, "main_net_pct": 10.0,
                          "flow": [], "amount": None, "turnover": 10.0}, [], 0)
    assert "3/5/30日资金流" in f3["missing"] and f3["day3"] is None, str(f3)


def _mk_min5() -> list[dict]:
    """10 根 5 分钟K：高开冲 10.6 后回落收 10.1（VWAP 10.2 下方 → 跳水结构 D）。"""
    return [{"date": "2026-09-04 09:35", "open": 10.3, "close": 10.5,
             "high": 10.6, "low": 10.3, "volume": 1e5},
            *[{"date": f"2026-09-04 09:{40+i}", "open": 10.4, "close": 10.3,
               "high": 10.4, "low": 10.2, "volume": 1e5} for i in range(4)],
            *[{"date": f"2026-09-04 10:{00+i}", "open": 10.2, "close": 10.1,
               "high": 10.2, "low": 10.0, "volume": 1e5} for i in range(5)]]


def test_minute_structure_diving() -> None:
    from backend import opportunity_screener as ops

    profile = {"price": 10.1, "vwap": 10.2, "prev_close": 10.0,
               "main_net_inflow": 5e6}
    minute = ops._minute_score(profile, _mk_min5(), {"hot_avg": 2.0})
    assert minute is not None, "分时数据不足应返回 None 而非空 dict"
    assert minute["structure"] == "D" and minute["score"] < 60, str(minute)
    assert "盘口五档主动买盘" in minute["missing"]  # 无源恒缺，如实标注

    # 数据不足（<8 根）→ None
    assert ops._minute_score(profile, _mk_min5()[:5], {}) is None


def test_divergence_and_gates() -> None:
    from backend import opportunity_screener as ops

    profile = {"price": 10.1, "vwap": 10.2, "prev_close": 10.0,
               "main_net_inflow": 5e6}
    minute = ops._minute_score(profile, _mk_min5(), {"hot_avg": 2.0})
    div = ops._divergence_score(minute, profile, {})
    assert div is not None and 0 <= div["score"] <= 100, str(div)
    assert div["appeared"] is True and div["fund_return"] is True, str(div)

    bstats = {"score": 85, "stage": "发酵"}
    yaogu = {"score": 75}
    fund = {"score": 75}
    ztp = {"value": 0.60}
    pmp = {"value": 0.70}
    # 分时 D 级 → score 低 → stock 门槛不过
    g = ops._gates(bstats, yaogu, fund, minute, ztp, pmp, composite=85)
    assert g["board"] is True and g["stock"] is False, str(g)
    # 全绿配置 → 三关全过
    minute_ok = {"score": 85, "structure": "A"}
    g2 = ops._gates(bstats, yaogu, fund, minute_ok, ztp, pmp, composite=85)
    assert all(g2.values()), str(g2)
    # 板块分不足 / 概率不足 → 分别拦下
    assert ops._gates({"score": 60, "stage": "发酵"}, yaogu, fund,
                      minute_ok, ztp, pmp, 85)["board"] is False
    assert ops._gates(bstats, yaogu, fund, minute_ok, {"value": 0.4}, pmp,
                      85)["prob"] is False


def test_exclusions_rules() -> None:
    from backend import opportunity_screener as ops

    # 干净画像：不触发任何排除
    clean = {"main_net_inflow": 5e7, "change_pct": 5.0, "lianban": 1, "flow": []}
    assert ops._exclusions(clean, {"zt_count": 5, "stage": "发酵"},
                           {"structure": "A"}, "A", 2.0, {"score": 80}) == []

    # 逐条触发
    b_ebb = {"zt_count": 5, "stage": "退潮"}
    e1 = ops._exclusions(clean, b_ebb, {"structure": "A"}, "A", 2.0, {"score": 80})
    assert any("排除1" in h for h in e1), e1

    out_flow = [{"main": -2e7}] * 3
    e2 = ops._exclusions({"main_net_inflow": -1e7, "change_pct": 5.0,
                          "lianban": 1, "flow": out_flow},
                         {"zt_count": 5, "stage": "发酵"}, {"structure": "A"},
                         "A", 2.0, {"score": 80})
    assert any("排除2" in h for h in e2), e2

    e3 = ops._exclusions(clean, {"zt_count": 5, "stage": "发酵"},
                         {"structure": "D"}, "A", 2.0, {"score": 80})
    assert any("排除3" in h for h in e3), e3

    e4 = ops._exclusions({**clean, "change_pct": 7.5, "lianban": 0},
                         {"zt_count": 0, "stage": "观察"}, {"structure": "A"},
                         "A", 2.0, {"score": 80})
    assert any("排除4" in h for h in e4), e4

    e6 = ops._exclusions(clean, {"zt_count": 5, "stage": "发酵"},
                         {"structure": "A"}, "A", 1.2, {"score": 80})
    assert any("排除6" in h for h in e6), e6

    e7 = ops._exclusions(clean, {"zt_count": 5, "stage": "发酵"},
                         {"structure": "A"}, "D", 2.0, {"score": 80})
    assert any("排除7" in h for h in e7), e7


def test_risk_reward_and_plan() -> None:
    from backend import opportunity_screener as ops

    rr = ops._risk_reward({"price": 10.0}, {"value": 0.6}, {"value": 0.7}, 9.5)
    # E_profit = 0.6*10 + 0.7*3 = 8.1；E_loss = (10-9.5)/10*100 = 5 → ratio 1.62
    assert rr["e_profit_pct"] == 8.1 and rr["e_loss_pct"] == 5.0, str(rr)
    assert rr["ratio"] == 1.62 and rr["assumed"] is False, str(rr)

    rr2 = ops._risk_reward({"price": 10.0}, {"value": 0.6}, {"value": 0.7}, None)
    assert rr2["assumed"] is True and rr2["e_loss_pct"] == 5.0, str(rr2)

    minute = {"feat": {"pullback_low": 10.1}}
    plan = ops._plan({"price": 10.0, "vwap": 10.2}, minute, "A")
    assert plan["stop1"].startswith("10.1"), str(plan)
    assert plan["max_position"] == "30%", str(plan)
    assert "14:30" in plan["endgame_cond"], str(plan)
    # D 级情绪 → 仓位 0%
    plan_d = ops._plan({"price": 10.0, "vwap": 10.2}, minute, "D")
    assert plan_d["max_position"] == "0%", str(plan_d)


def test_chg_n() -> None:
    from backend import opportunity_screener as ops

    bars = [{"close": 10.0}] * 5 + [{"close": 11.0}]
    assert ops._chg_n(bars, 5) == 10.0
    assert ops._chg_n(bars[:5], 5) is None  # 数据不足
