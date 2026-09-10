"""Indicators。"""
from __future__ import annotations

from tests._common import *  # noqa: F401,F403  公共导入见 tests/_common.py
from backend.indicators import build_ma, summarize_flow, support_resistance
from backend.providers import registry
from backend.providers.base import Bar, FlowDay

def test_kline_stale() -> None:
    from datetime import datetime
    from backend.utils import kline_is_stale

    # 固定注入时间，避免 CI 在任意时刻运行时判据漂移
    mon_close = datetime(2026, 8, 17, 16, 0)   # 周一收盘后
    # 周一收盘：K线停在周五应判滞后，含今天则正常
    assert (kline_is_stale("2026-08-14", mon_close) is True)
    assert (kline_is_stale("2026-08-17", mon_close) is False)
    assert (kline_is_stale("2026-08-14", datetime(2026, 8, 17, 11, 0)) is False)
    assert (kline_is_stale("", mon_close) is False)
    assert (kline_is_stale("abc", mon_close) is False)





def test_indicators() -> None:
    bars = [
        Bar(date=f"2026-01-{i:02d}", open=10 + i, close=10 + i * 0.5, high=12 + i, low=9 + i, volume=100.0)
        for i in range(1, 70)
    ]
    infos, summary = build_ma(bars, 20.0)
    assert (len(infos) == 4), str(len(infos))
    assert ("arrangement" in summary and "series" in summary)
    ma_values = {i.window: i.value for i in infos}
    sr = support_resistance(bars, 20.0, ma_values)
    assert (bool(sr.get("support") and sr.get("resistance"))), str(sr.get("state"))

    # 资金流向当日新鲜度：ref_date=K线最新日期
    from backend.providers.base import FlowDay
    flow_rows = [
        FlowDay(date="2026-08-14", main=-1.8e8, sm=0, md=0, lg=0, xl=-1.3e8),
        FlowDay(date="2026-08-17", main=2.4e8, sm=0, md=0, lg=0, xl=2.9e8),
        FlowDay(date="2026-08-18", main=-2.45e8, sm=0, md=0, lg=0, xl=-2.58e8),
    ]
    f_fresh = summarize_flow(flow_rows, ref_date="2026-08-18")
    assert (f_fresh["fresh"] is True and f_fresh["last_date"] == "2026-08-18"), str(f_fresh.get("fresh"))
    assert (f_fresh["state"] == "主力净流出（当日）"), f_fresh["state"]
    # 模拟 16 点前：最后一行是 17 日（昨日）且为流出，K线已到 18 日
    flow_y = [
        FlowDay(date="2026-08-14", main=1.8e8, sm=0, md=0, lg=0, xl=1.3e8),
        FlowDay(date="2026-08-17", main=-2.4e8, sm=0, md=0, lg=0, xl=-2.9e8),
    ]
    f_y = summarize_flow(flow_y, ref_date="2026-08-18")
    assert (f_y["fresh"] is False and f_y["last_date"] == "2026-08-17"), str(f_y.get("fresh"))
    assert (f_y["state"] == "主力净流出（近5日）"), f_y["state"]

    # --------------------- 5 档分级 + 价量背离 + 主力类型 ---------------------

    # 主力抢筹：连入 3 日 + 超大单主导（机构抢筹）+ 共振看多
    # 至少 10 日数据才能计算价量背离（前 5 日 vs 后 5 日对比）
    flow_strong_in = [
        FlowDay(date="2026-07-29", main=1.0e8, sm=0, md=0, lg=0, xl=0.9e8, close=9.0),
        FlowDay(date="2026-07-30", main=1.2e8, sm=0, md=0, lg=0, xl=1.0e8, close=9.2),
        FlowDay(date="2026-07-31", main=1.1e8, sm=0, md=0, lg=0, xl=1.0e8, close=9.4),
        FlowDay(date="2026-08-03", main=1.3e8, sm=0, md=0, lg=0, xl=1.1e8, close=9.6),
        FlowDay(date="2026-08-04", main=1.0e8, sm=0, md=0, lg=0, xl=0.9e8, close=9.8),
        FlowDay(date="2026-08-11", main=2.0e8, sm=0, md=0, lg=0, xl=1.8e8, close=10.0),
        FlowDay(date="2026-08-12", main=3.0e8, sm=0, md=0, lg=0, xl=2.7e8, close=10.2),
        FlowDay(date="2026-08-13", main=2.5e8, sm=0, md=0, lg=0, xl=2.4e8, close=10.5),
        FlowDay(date="2026-08-14", main=2.8e8, sm=0, md=0, lg=0, xl=2.6e8, close=10.8),
        FlowDay(date="2026-08-17", main=2.2e8, sm=0, md=0, lg=0, xl=2.0e8, close=11.0),
        FlowDay(date="2026-08-18", main=3.0e8, sm=0, md=0, lg=0, xl=2.9e8, close=11.3),
    ]
    f_si = summarize_flow(flow_strong_in, ref_date="2026-08-18")
    # v4: 强共振信号时, base 标签会追加「·共振看多」后缀
    assert (f_si["state"].startswith("主力抢筹（当日）") and f_si["state_grade"] == "inflow"), f_si["state"]
    assert ("机构主导" in (f_si.get("xl_dominance") or "")), str(f_si.get("xl_dominance"))
    assert ("共振看多" in (f_si.get("price_flow_note") or "")), str(f_si.get("price_flow_note"))
    assert ("·共振看多" in f_si["state"] or "主力抢筹（当日）" == f_si["state"]), f_si["state"]

    # 主力出逃：连出 3 日 + 超大单主导 + 共振看空
    flow_strong_out = [
        FlowDay(date="2026-07-29", main=-1.0e8, sm=0, md=0, lg=0, xl=-0.9e8, close=11.8),
        FlowDay(date="2026-07-30", main=-1.2e8, sm=0, md=0, lg=0, xl=-1.0e8, close=11.6),
        FlowDay(date="2026-07-31", main=-1.1e8, sm=0, md=0, lg=0, xl=-1.0e8, close=11.4),
        FlowDay(date="2026-08-03", main=-1.3e8, sm=0, md=0, lg=0, xl=-1.1e8, close=11.2),
        FlowDay(date="2026-08-04", main=-1.0e8, sm=0, md=0, lg=0, xl=-0.9e8, close=11.0),
        FlowDay(date="2026-08-11", main=-2.0e8, sm=0, md=0, lg=0, xl=-1.8e8, close=11.5),
        FlowDay(date="2026-08-12", main=-3.0e8, sm=0, md=0, lg=0, xl=-2.7e8, close=11.3),
        FlowDay(date="2026-08-13", main=-2.5e8, sm=0, md=0, lg=0, xl=-2.4e8, close=11.0),
        FlowDay(date="2026-08-14", main=-2.8e8, sm=0, md=0, lg=0, xl=-2.6e8, close=10.8),
        FlowDay(date="2026-08-17", main=-2.2e8, sm=0, md=0, lg=0, xl=-2.0e8, close=10.5),
        FlowDay(date="2026-08-18", main=-3.0e8, sm=0, md=0, lg=0, xl=-2.9e8, close=10.2),
    ]
    f_so = summarize_flow(flow_strong_out, ref_date="2026-08-18")
    # v4: 强共振信号时, base 标签会追加「·共振看空」后缀
    assert (f_so["state"].startswith("主力出逃（当日）") and f_so["state_grade"] == "outflow"), f_so["state"]
    assert ("共振看空" in (f_so.get("price_flow_note") or "")), str(f_so.get("price_flow_note"))
    assert ("·共振看空" in f_so["state"] or "主力出逃（当日）" == f_so["state"]), f_so["state"]

    # 普通流入（非连入/超大单主导） → 主力净流入
    flow_plain_in = [
        FlowDay(date="2026-08-15", main=1.0e8, sm=0, md=0, lg=0, xl=0.3e8, close=10.0),
        FlowDay(date="2026-08-16", main=1.5e8, sm=0, md=0, lg=0, xl=0.4e8, close=10.1),
        FlowDay(date="2026-08-17", main=0.8e8, sm=0, md=0, lg=0, xl=0.2e8, close=10.3),
        FlowDay(date="2026-08-18", main=1.2e8, sm=0, md=0, lg=0, xl=0.3e8, close=10.5),
    ]
    f_pi = summarize_flow(flow_plain_in, ref_date="2026-08-18")
    assert (f_pi["state"] == "主力净流入（当日）" and f_pi["state_grade"] == "inflow"), f_pi["state"]
    assert ("主力分散" in (f_pi.get("xl_dominance") or "")), str(f_pi.get("xl_dominance"))

    # 价量背离：价格↑ 资金↓ = 高位诱多
    flow_divergence = [
        # 前5日：价格低、资金高（吸筹期）
        FlowDay(date="2026-08-11", main=2.0e8, sm=0, md=0, lg=0, xl=1.5e8, close=9.0),
        FlowDay(date="2026-08-12", main=2.5e8, sm=0, md=0, lg=0, xl=2.0e8, close=9.2),
        FlowDay(date="2026-08-13", main=3.0e8, sm=0, md=0, lg=0, xl=2.5e8, close=9.4),
        FlowDay(date="2026-08-14", main=2.8e8, sm=0, md=0, lg=0, xl=2.3e8, close=9.6),
        FlowDay(date="2026-08-17", main=2.2e8, sm=0, md=0, lg=0, xl=1.8e8, close=9.8),
        # 后5日：价格上涨但资金流出（诱多）
        FlowDay(date="2026-08-18", main=-1.5e8, sm=0, md=0, lg=0, xl=-1.2e8, close=10.2),
        FlowDay(date="2026-08-19", main=-2.0e8, sm=0, md=0, lg=0, xl=-1.8e8, close=10.5),
        FlowDay(date="2026-08-20", main=-2.5e8, sm=0, md=0, lg=0, xl=-2.2e8, close=10.8),
        FlowDay(date="2026-08-21", main=-2.0e8, sm=0, md=0, lg=0, xl=-1.7e8, close=11.0),
        FlowDay(date="2026-08-24", main=-1.8e8, sm=0, md=0, lg=0, xl=-1.5e8, close=11.3),
    ]
    f_dv = summarize_flow(flow_divergence, ref_date="2026-08-24")
    assert ("高位诱多" in (f_dv.get("price_flow_note") or "")), str(f_dv.get("price_flow_note"))

    # 价量背离：价格↓ 资金↑ = 低位吸筹
    flow_absorb = [
        FlowDay(date="2026-08-11", main=-2.0e8, sm=0, md=0, lg=0, xl=-1.5e8, close=11.5),
        FlowDay(date="2026-08-12", main=-2.5e8, sm=0, md=0, lg=0, xl=-2.0e8, close=11.3),
        FlowDay(date="2026-08-13", main=-3.0e8, sm=0, md=0, lg=0, xl=-2.5e8, close=11.0),
        FlowDay(date="2026-08-14", main=-2.8e8, sm=0, md=0, lg=0, xl=-2.3e8, close=10.8),
        FlowDay(date="2026-08-17", main=-2.2e8, sm=0, md=0, lg=0, xl=-1.8e8, close=10.5),
        FlowDay(date="2026-08-18", main=1.5e8, sm=0, md=0, lg=0, xl=1.2e8, close=10.2),
        FlowDay(date="2026-08-19", main=2.0e8, sm=0, md=0, lg=0, xl=1.8e8, close=10.0),
        FlowDay(date="2026-08-20", main=2.5e8, sm=0, md=0, lg=0, xl=2.2e8, close=9.8),
        FlowDay(date="2026-08-21", main=2.0e8, sm=0, md=0, lg=0, xl=1.7e8, close=9.6),
        FlowDay(date="2026-08-24", main=1.8e8, sm=0, md=0, lg=0, xl=1.5e8, close=9.4),
    ]
    f_ab = summarize_flow(flow_absorb, ref_date="2026-08-24")
    assert ("低位吸筹" in (f_ab.get("price_flow_note") or "")), str(f_ab.get("price_flow_note"))

    # 新浪兜底源 xl=0 → 主力类型应为空
    flow_sina = [
        FlowDay(date="2026-08-17", main=1.5e8, sm=0.3e8, md=0, lg=0, xl=0, close=10.0),
        FlowDay(date="2026-08-18", main=2.0e8, sm=0.5e8, md=0, lg=0, xl=0, close=10.2),
    ]
    f_sina = summarize_flow(flow_sina, ref_date="2026-08-18")
    assert (not f_sina.get("xl_dominance")), str(f_sina.get("xl_dominance"))

    # 资金观望：main_last ≈ 0
    flow_watch = [
        FlowDay(date="2026-08-17", main=0.5e7, sm=0, md=0, lg=0, xl=0.3e7, close=10.0),
        FlowDay(date="2026-08-18", main=0.0, sm=0, md=0, lg=0, xl=0.0, close=10.1),
    ]
    f_w = summarize_flow(flow_watch, ref_date="2026-08-18")
    assert (f_w["state"] == "资金观望（当日）" and f_w["state_grade"] == "neutral"), f_w["state"]

    # 样本太少时不计算背离
    flow_short = [
        FlowDay(date="2026-08-17", main=1.0e8, sm=0, md=0, lg=0, xl=0.5e8, close=10.0),
        FlowDay(date="2026-08-18", main=1.5e8, sm=0, md=0, lg=0, xl=0.8e8, close=10.2),
    ]
    f_sh = summarize_flow(flow_short, ref_date="2026-08-18")
    assert (not f_sh.get("price_flow_note")), str(f_sh.get("price_flow_note"))
    # ---- v4: 价量对齐标注 / 趋势反转 / 主力类型权重 ----

    # 抢筹 + 高位诱多: 后期资金均量缩水但价格续涨
    flow_sid = [
        FlowDay(date="2026-07-29", main=5.0e8, sm=0, md=0, lg=0, xl=4.0e8, close=10.0),
        FlowDay(date="2026-07-30", main=4.5e8, sm=0, md=0, lg=0, xl=3.8e8, close=10.2),
        FlowDay(date="2026-07-31", main=5.5e8, sm=0, md=0, lg=0, xl=4.5e8, close=10.4),
        FlowDay(date="2026-08-03", main=4.8e8, sm=0, md=0, lg=0, xl=4.0e8, close=10.6),
        FlowDay(date="2026-08-04", main=5.2e8, sm=0, md=0, lg=0, xl=4.3e8, close=10.8),
        FlowDay(date="2026-08-11", main=1.0e8, sm=0, md=0, lg=0, xl=0.5e8, close=11.5),
        FlowDay(date="2026-08-12", main=0.8e8, sm=0, md=0, lg=0, xl=0.4e8, close=11.8),
        FlowDay(date="2026-08-13", main=1.2e8, sm=0, md=0, lg=0, xl=0.6e8, close=12.0),
        FlowDay(date="2026-08-14", main=0.9e8, sm=0, md=0, lg=0, xl=0.4e8, close=12.3),
        FlowDay(date="2026-08-17", main=1.1e8, sm=0, md=0, lg=0, xl=0.5e8, close=12.6),
        FlowDay(date="2026-08-18", main=1.0e8, sm=0, md=0, lg=0, xl=0.5e8, close=12.9),
    ]
    f_sid = summarize_flow(flow_sid, ref_date="2026-08-18")
    assert ("高位诱多" in f_sid["state"] and f_sid["state_grade"] == "inflow"), f_sid["state"]
    assert (f_sid.get("price_flow_note") == "价格↑资金↓ 高位诱多"), str(f_sid.get("price_flow_note"))

    # 抢筹 + 低位吸筹: 后5日资金大额流入但价格继续下跌
    flow_abs = [
        FlowDay(date="2026-07-29", main=-3.0e8, sm=0, md=0, lg=0, xl=-2.5e8, close=15.0),
        FlowDay(date="2026-07-30", main=-2.5e8, sm=0, md=0, lg=0, xl=-2.0e8, close=14.8),
        FlowDay(date="2026-07-31", main=-3.5e8, sm=0, md=0, lg=0, xl=-3.0e8, close=14.5),
        FlowDay(date="2026-08-03", main=-2.8e8, sm=0, md=0, lg=0, xl=-2.3e8, close=14.0),
        FlowDay(date="2026-08-04", main=-3.2e8, sm=0, md=0, lg=0, xl=-2.7e8, close=13.5),
        FlowDay(date="2026-08-11", main=2.5e8, sm=0, md=0, lg=0, xl=2.0e8, close=13.0),
        FlowDay(date="2026-08-12", main=3.0e8, sm=0, md=0, lg=0, xl=2.5e8, close=12.8),
        FlowDay(date="2026-08-13", main=2.8e8, sm=0, md=0, lg=0, xl=2.3e8, close=12.5),
        FlowDay(date="2026-08-14", main=3.5e8, sm=0, md=0, lg=0, xl=3.0e8, close=12.2),
        FlowDay(date="2026-08-17", main=3.2e8, sm=0, md=0, lg=0, xl=2.7e8, close=11.9),
        FlowDay(date="2026-08-18", main=3.8e8, sm=0, md=0, lg=0, xl=3.2e8, close=11.5),
    ]
    f_abs = summarize_flow(flow_abs, ref_date="2026-08-18")
    assert ("低位吸筹" in f_abs["state"] and f_abs["state_grade"] == "inflow"), f_abs["state"]

    # 出逃 + 共振看空: 价格大跌+资金大额流出
    flow_or = [
        FlowDay(date="2026-07-29", main=-0.5e8, sm=0, md=0, lg=0, xl=-0.4e8, close=12.0),
        FlowDay(date="2026-07-30", main=-0.4e8, sm=0, md=0, lg=0, xl=-0.3e8, close=11.9),
        FlowDay(date="2026-07-31", main=-0.6e8, sm=0, md=0, lg=0, xl=-0.5e8, close=11.8),
        FlowDay(date="2026-08-03", main=-0.5e8, sm=0, md=0, lg=0, xl=-0.4e8, close=11.7),
        FlowDay(date="2026-08-04", main=-0.4e8, sm=0, md=0, lg=0, xl=-0.3e8, close=11.6),
        FlowDay(date="2026-08-11", main=-2.0e8, sm=0, md=0, lg=0, xl=-1.8e8, close=11.0),
        FlowDay(date="2026-08-12", main=-3.0e8, sm=0, md=0, lg=0, xl=-2.7e8, close=10.5),
        FlowDay(date="2026-08-13", main=-2.5e8, sm=0, md=0, lg=0, xl=-2.3e8, close=10.0),
        FlowDay(date="2026-08-14", main=-3.5e8, sm=0, md=0, lg=0, xl=-3.2e8, close=9.5),
        FlowDay(date="2026-08-17", main=-3.0e8, sm=0, md=0, lg=0, xl=-2.8e8, close=9.0),
        FlowDay(date="2026-08-18", main=-4.0e8, sm=0, md=0, lg=0, xl=-3.7e8, close=8.5),
    ]
    f_or = summarize_flow(flow_or, ref_date="2026-08-18")
    assert ("共振看空" in f_or["state"] and f_or["state_grade"] == "outflow"), f_or["state"]

    # 趋势反转: 当日流出但累计/近5日均流入 → 信号不稳,不轻易报出逃
    flow_rev = [
        FlowDay(date="2026-08-11", main=2.0e8, sm=0, md=0, lg=0, xl=1.8e8, close=10.0),
        FlowDay(date="2026-08-12", main=3.0e8, sm=0, md=0, lg=0, xl=2.7e8, close=10.2),
        FlowDay(date="2026-08-13", main=2.5e8, sm=0, md=0, lg=0, xl=2.3e8, close=10.5),
        FlowDay(date="2026-08-14", main=2.8e8, sm=0, md=0, lg=0, xl=2.5e8, close=10.7),
        FlowDay(date="2026-08-17", main=1.5e8, sm=0, md=0, lg=0, xl=1.2e8, close=10.9),
        FlowDay(date="2026-08-18", main=-4.0e8, sm=0, md=0, lg=0, xl=-3.5e8, close=10.6),
    ]
    f_rev = summarize_flow(flow_rev, ref_date="2026-08-18")
    assert ("出逃" not in f_rev["state"]), f_rev["state"]

    # 机构主导 vs 主力分散: 同等 streak 但 xl 占比不同 → 分级不同
    flow_inst = [
        FlowDay(date="2026-08-15", main=1.0e8, sm=0, md=0, lg=0, xl=0.9e8, close=10.0),
        FlowDay(date="2026-08-16", main=1.2e8, sm=0, md=0, lg=0, xl=1.1e8, close=10.2),
        FlowDay(date="2026-08-17", main=1.1e8, sm=0, md=0, lg=0, xl=1.0e8, close=10.4),
        FlowDay(date="2026-08-18", main=1.3e8, sm=0, md=0, lg=0, xl=1.2e8, close=10.6),
    ]
    flow_retail = [
        FlowDay(date="2026-08-15", main=1.0e8, sm=0, md=0, lg=0, xl=0.2e8, close=10.0),
        FlowDay(date="2026-08-16", main=1.2e8, sm=0, md=0, lg=0, xl=0.3e8, close=10.2),
        FlowDay(date="2026-08-17", main=1.1e8, sm=0, md=0, lg=0, xl=0.2e8, close=10.4),
        FlowDay(date="2026-08-18", main=1.3e8, sm=0, md=0, lg=0, xl=0.3e8, close=10.6),
    ]
    f_inst = summarize_flow(flow_inst, ref_date="2026-08-18")
    f_retail = summarize_flow(flow_retail, ref_date="2026-08-18")
    assert ("抢筹" in f_inst["state"] and "抢筹" not in f_retail["state"]), f"inst={f_inst['state']} | retail={f_retail['state']}"

    # _tone_flow 染色覆盖
    from backend.indicators import _tone_flow
    assert (_tone_flow("主力抢筹") == "up"), _tone_flow("主力抢筹")
    assert (_tone_flow("主力净流入") == "up"), _tone_flow("主力净流入")
    assert (_tone_flow("主力净流入（近5日）") == "up"), _tone_flow("主力净流入（近5日）")
    assert (_tone_flow("主力出逃") == "down"), _tone_flow("主力出逃")
    assert (_tone_flow("主力净流出") == "down"), _tone_flow("主力净流出")
    assert (_tone_flow("资金观望") == "flat"), _tone_flow("资金观望")

    # --------------------- ATR(14) + 支撑压力 ATR 突破 + 趋势 ATR 归一化 ---------------------
    from backend.indicators import compute_atr, decorate_bars_with_atr, trend_state

    # ATR 基础：70 根单调上涨 bar，TR 始终 = high-low，ATR(14) ≈ 单根 TR
    bars70 = [
        Bar(date=f"2026-01-{i:02d}", open=10 + i, close=10 + i * 0.5,
            high=12 + i, low=9 + i, volume=100.0)
        for i in range(1, 71)
    ]
    atr_seq = compute_atr(bars70, period=14)
    # 前 13 根（含第 0 根）应为 None，从第 13 根起开始有 ATR
    assert (all(v is None for v in atr_seq[:13])), str([v for v in atr_seq[:14]])
    # 至少从第 14 根起全部有 ATR（包含最后）
    assert (atr_seq[13] is not None and atr_seq[-1] is not None), f"atr[13]={atr_seq[13]}, atr[-1]={atr_seq[-1]}"
    # 样本不足时全 None
    atr_short = compute_atr(bars70[:5], period=14)
    assert (all(v is None for v in atr_short)), str(atr_short)

    # decorate_bars_with_atr 原地写入并返回最新值
    bars_copy = [Bar(date=b.date, open=b.open, close=b.close, high=b.high, low=b.low, volume=b.volume)
                 for b in bars70[:30]]
    last_atr = decorate_bars_with_atr(bars_copy, period=14)
    assert (all(b.atr is not None for b in bars_copy[13:]) and bars_copy[12].atr is None), f"last_atr={last_atr}"
    assert (last_atr is not None and last_atr > 0), str(last_atr)

    # 支撑压力 ATR 突破：构造 20 日区间 [10, 11]、ATR=1.5、price=11.95
    #   突破容差 = max(price*0.5%, 0.5*ATR) = max(0.06, 0.75) = 0.75
    #   price >= high20 + tol => 11 + 0.75 = 11.75 → 11.95 已突破
    sr_breach = [
        Bar(date=f"2026-01-{i:02d}", open=10.5, close=10.5, high=11.0, low=10.0, volume=100.0)
        for i in range(1, 21)
    ]
    sr_breach_dict = support_resistance(sr_breach, 11.95, {}, atr=1.5)
    assert (sr_breach_dict["state"] == "突破区间上沿" and sr_breach_dict["atr_breakout"] == "已突破"), f"state={sr_breach_dict['state']}, flag={sr_breach_dict['atr_breakout']}"

    # 支撑压力 ATR 跌破：price=9.05，ATR=1.5，tol=0.75
    #   price <= low20 - tol => 10 - 0.75 = 9.25 → 9.05 已跌破
    sr_break_down = support_resistance(sr_breach, 9.05, {}, atr=1.5)
    assert (sr_break_down["state"] == "跌破区间下沿" and sr_break_down["atr_breakout"] == "已跌破"), f"state={sr_break_down['state']}, flag={sr_break_down['atr_breakout']}"

    # 支撑压力 ATR 逼近：price=11.3，距 high20=11 差距 0.3 < tol=0.75 → 逼近
    sr_near = support_resistance(sr_breach, 11.3, {}, atr=1.5)
    assert (sr_near["state"] == "逼近压力位" and sr_near["atr_breakout"] == "逼近"), f"state={sr_near['state']}, flag={sr_near['atr_breakout']}"

    # 支撑压力 ATR 不可用：退化为 price*0.5% 容差
    #   price=11.07，tol=11.07*0.005=0.0554，high20=11 > 11.07-0.0554=11.015 → 突破
    sr_noatr = support_resistance(sr_breach, 11.07, {}, atr=None)
    assert (sr_noatr["state"] == "突破区间上沿" and sr_noatr["atr"] is None
          and sr_noatr["atr_breakout"] == "已突破"), f"state={sr_noatr['state']}, atr={sr_noatr['atr']}"

    # 趋势 ATR 归一化：构造近 5 日涨 1.2%，ATR=0.5（占股价 2.5%），sqrt(5)≈2.236
    #   unit_atr = 0.012 / (0.025 * 2.236) ≈ 0.215 → 震荡
    #   若 ATR=0.05（占 0.1%），则 unit_atr ≈ 53.7 → 上涨
    flat_bars = [
        Bar(date=f"2026-01-{i:02d}", open=20.0, close=20.0, high=20.05, low=19.95, volume=100.0)
        for i in range(1, 67)
    ]
    # 把最后 5 根小幅推高 1.2%，让 chg_5d ≈ +1.2
    for i in range(5):
        flat_bars[-(i + 1)].close = 20.0 + 0.024 * (5 - i)  # 推高 0.024/0.048/.../0.12
    t_high_vol = trend_state(flat_bars, {"arrangement": "均线交织", "above_count": 0}, atr=0.5)
    assert (t_high_vol["short"] == "震荡" and t_high_vol["atr_normalized"] is True), f"short={t_high_vol['short']}, atr_norm={t_high_vol['atr_normalized']}"

    t_low_vol = trend_state(flat_bars, {"arrangement": "均线交织", "above_count": 0}, atr=0.05)
    assert (t_low_vol["short"] == "上涨" and t_low_vol["atr_normalized"] is True), f"short={t_low_vol['short']}"

    # 趋势 ATR 不可用：退化为原 ±2%/±5%/±8% 阈值
    t_noatr = trend_state(flat_bars, {"arrangement": "均线交织", "above_count": 0}, atr=None)
    assert (t_noatr["atr_normalized"] is False
          and t_noatr["atr"] is None
          and t_noatr["vol_unit_atr"]["chg_5d"] is None), f"atr_norm={t_noatr['atr_normalized']}, atr={t_noatr['atr']}"

    # --------------------- P0/P1: 状态标签准确性 + 时效性 ---------------------

    # P1-4：支撑压力次要支撑/压力列表
    sr_secondary = support_resistance(sr_breach, 11.5, {}, atr=1.0)
    assert (isinstance(sr_secondary.get("secondary_support"), list)
          and isinstance(sr_secondary.get("secondary_resistance"), list)), str({k: sr_secondary.get(k) for k in ("secondary_support", "secondary_resistance")})

    # P1-5：量能验证（缩量/放量/平量）
    from backend.indicators import trend_state as _trend_state
    flat_bars2 = [
        Bar(date=f"2026-03-{i:02d}", open=20.0, close=20.0, high=20.05, low=19.95, volume=100.0)
        for i in range(1, 67)
    ]
    for i in range(5):
        flat_bars2[-(i + 1)].close = 20.0 + 0.024 * (5 - i)
    # 平量：最后一天 volume=100（与近 5 日均量相等）
    t_vol_flat = _trend_state(flat_bars2, {"arrangement": "均线交织", "above_count": 0}, atr=None)
    assert (t_vol_flat["volume_confirm"] == "平量" and t_vol_flat["vol_5d_ratio"] is not None), f"vc={t_vol_flat['volume_confirm']}, ratio={t_vol_flat['vol_5d_ratio']}"

    # 放量：最后一天 volume=300
    flat_bars3 = [Bar(date=b.date, open=b.open, close=b.close, high=b.high, low=b.low, volume=b.volume)
                  for b in flat_bars2]
    flat_bars3[-1].volume = 300.0
    t_vol_up = _trend_state(flat_bars3, {"arrangement": "均线交织", "above_count": 0}, atr=None)
    assert (t_vol_up["volume_confirm"] == "放量" and t_vol_up["vol_5d_ratio"] >= 1.3), f"vc={t_vol_up['volume_confirm']}, ratio={t_vol_up['vol_5d_ratio']}"

    # 缩量：最后一天 volume=10
    flat_bars4 = [Bar(date=b.date, open=b.open, close=b.close, high=b.high, low=b.low, volume=b.volume)
                  for b in flat_bars2]
    flat_bars4[-1].volume = 10.0
    t_vol_down = _trend_state(flat_bars4, {"arrangement": "均线交织", "above_count": 0}, atr=None)
    assert (t_vol_down["volume_confirm"] == "缩量" and t_vol_down["vol_5d_ratio"] <= 0.7), f"vc={t_vol_down['volume_confirm']}, ratio={t_vol_down['vol_5d_ratio']}"

    # P0-2：两融 sentiment_with_date 带披露日期
    from backend.indicators import summarize_margin
    from backend.providers.base import MarginDay
    margin_rows = [
        MarginDay(date="2026-08-20", rzye=1500000000.0, rqye=10000000.0),
        MarginDay(date="2026-08-21", rzye=1550000000.0, rqye=11000000.0),
        MarginDay(date="2026-08-25", rzye=1600000000.0, rqye=12000000.0),
    ]
    margin_sum = summarize_margin(margin_rows)
    assert ("2026-08-25" in margin_sum["sentiment_with_date"]), margin_sum["sentiment_with_date"]
    assert ("2026" not in margin_sum["sentiment"]), margin_sum["sentiment"]
    assert (margin_sum["last_date"] == "2026-08-25"), str(margin_sum["last_date"])

    # P0-1/P0-3：build_status 三种模式
    from backend.indicators import build_status
    from backend.providers.base import Quote

    # 准备一份能产生正常标签的 quote+bars+summary
    long_bars = [
        Bar(date=f"2026-06-{i:02d}", open=10 + i * 0.1, close=10 + i * 0.1 + 0.05,
            high=10 + i * 0.1 + 0.1, low=10 + i * 0.1 - 0.05, volume=100.0)
        for i in range(1, 31)
    ]
    long_bars = [
        Bar(date=f"2026-{m:02d}-{d:02d}", open=10 + i * 0.1, close=10 + i * 0.1 + 0.05,
            high=10 + i * 0.1 + 0.1, low=10 + i * 0.1 - 0.05, volume=100.0)
        for i, (m, d) in enumerate(((6, d) for d in range(1, 31)), 1)
    ]
    quote = Quote(code="600000", market="SH", price=11.0, prev_close=10.95, change_pct=0.46,
                  status="normal")
    trend_normal = trend_state(long_bars, {"arrangement": "均线交织", "above_count": 2})
    flow_normal = {"state": "主力净流入", "available": True}
    sr_normal = {"state": "区间中枢震荡"}
    margin_normal = {"sentiment": "两融情绪平稳",
                     "sentiment_with_date": "两融情绪平稳（截至 2026-08-25）"}

    # 正常模式
    st_normal = build_status(quote, long_bars, flow_normal, margin_normal,
                              {"arrangement": "均线交织"}, sr_normal)
    tag_groups = {t["group"]: t for t in st_normal["tags"]}
    assert ("2026-08-25" in tag_groups["两融情绪"]["label"]), tag_groups["两融情绪"]["label"]
    assert (tag_groups["趋势状态"]["tone"] != "warn"), str(tag_groups["趋势状态"])

    # 盘前模式
    st_pre = build_status(quote, long_bars, flow_normal, margin_normal,
                           {"arrangement": "均线交织"}, sr_normal, pre_open=True)
    pre_groups = {t["group"]: t for t in st_pre["tags"]}
    assert (all(pre_groups[g]["label"] == "待开盘" for g in
              ("趋势状态", "资金状态", "支撑压力", "两融情绪", "均线形态"))), str({g: pre_groups[g]["label"] for g in pre_groups})
    assert (all(pre_groups[g]["tone"] == "warn" for g in
              ("趋势状态", "资金状态", "支撑压力", "两融情绪", "均线形态"))), str({g: pre_groups[g]["tone"] for g in pre_groups})

    # 延迟模式
    st_delayed = build_status(quote, long_bars, flow_normal, margin_normal,
                               {"arrangement": "均线交织"}, sr_normal, delayed=True)
    del_groups = {t["group"]: t for t in st_delayed["tags"]}
    assert (del_groups["趋势状态"]["tone"] == "warn"), str(del_groups["趋势状态"])
    assert ("2026-08-25" in del_groups["两融情绪"]["label"]), del_groups["两融情绪"]["label"]

    # P1-6：streak_text 出现在 summary 里
    # flow_strong_in 实际是连续 11 日流入，这里验证格式正确（连 N 日流入/流出）
    flow_with_streak = summarize_flow(flow_strong_in, ref_date="2026-08-18")
    assert (flow_with_streak["streak_text"] == "连 11 日流入"), str(flow_with_streak.get("streak_text"))
    # 反向：连出场景
    flow_with_streak_out = summarize_flow(flow_strong_out, ref_date="2026-08-18")
    assert (flow_with_streak_out["streak_text"] == "连 11 日流出"), str(flow_with_streak_out.get("streak_text"))

    # --------------------- P2: 分钟级 K 线 + ATR 区间阈值 ---------------------

    # P2-7：intraday_trend_state 各种场景
    from backend.indicators import intraday_trend_state

    # 数据不足：少于 6 根 → available=False
    short_bars = [Bar(date=f"2026-08-26 {h:02d}:00", open=10.0, close=10.0 + h * 0.01,
                       high=10.05, low=9.95, volume=100.0) for h in range(5)]
    intraday_short = intraday_trend_state(short_bars)
    assert (intraday_short["available"] is False and intraday_short["bars"] == 5), str(intraday_short)

    # 上涨：首 10 → 末 10.5，+5%
    up_bars = [Bar(date=f"2026-08-26 {h:02d}:00", open=10.0, close=10.0 + h * 0.05,
                    high=10.05 + h * 0.05, low=9.95 + h * 0.05, volume=100.0) for h in range(10)]
    intraday_up = intraday_trend_state(up_bars)
    assert (intraday_up["available"] is True and intraday_up["label"] == "上涨"), str(intraday_up)

    # 下跌：首 10 → 末 9.5，-5%
    down_bars = [Bar(date=f"2026-08-26 {h:02d}:00", open=10.0, close=10.0 - h * 0.05,
                      high=10.05, low=9.95 - h * 0.05, volume=100.0) for h in range(10)]
    intraday_down = intraday_trend_state(down_bars)
    assert (intraday_down["available"] is True and intraday_down["label"] == "下跌"), str(intraday_down)

    # 震荡：变化仅 0.5%
    flat_min = [Bar(date=f"2026-08-26 {h:02d}:00", open=10.0, close=10.0 + h * 0.0005,
                     high=10.005, low=9.995, volume=100.0) for h in range(10)]
    intraday_flat = intraday_trend_state(flat_min)
    assert (intraday_flat["available"] is True and intraday_flat["label"] == "震荡"), str(intraday_flat)

    # build_status 注入 intraday 子标签
    st_intra_up = build_status(quote, long_bars, flow_normal, margin_normal,
                                {"arrangement": "均线交织"}, sr_normal,
                                intraday=intraday_up)
    intra_tags = [t for t in st_intra_up["tags"] if t["group"] == "盘中实时"]
    assert (len(intra_tags) == 1
          and "60分线" in intra_tags[0]["label"]
          and intra_tags[0]["tone"] == "up"), str(intra_tags)

    # intraday 不可用（available=False）→ 不应有"盘中实时"标签
    st_intra_na = build_status(quote, long_bars, flow_normal, margin_normal,
                                {"arrangement": "均线交织"}, sr_normal,
                                intraday=intraday_short)
    na_tags = [t for t in st_intra_na["tags"] if t["group"] == "盘中实时"]
    assert (len(na_tags) == 0), str(na_tags)

    # P2-8：区间位置 ATR 归一化
    # 高 ATR（atr=2，区间宽 1）：atr_band=8，upper=max(0.66,8)=8，lower=min(0.34,-7)=-7
    # 这意味着任何 price 都会落入区间（被"中枢震荡"覆盖），实际效果是：高波动股票
    # 在区间内不会被轻易判定到上下半区，体现 ATR 归一化的语义
    # 但由于 ATR 也参与 tol 计算（tol=price*0.5% 与 0.5*atr 取较大者），当 atr=2 时
    # tol=1.0，导致 price=10.4 距 high20=11 仅 0.6 元 < 1.0 容差，先被判"逼近压力位"
    # 所以高 ATR 下要走逼近分支（这是设计正确的：高波动股票 ATR 容差宽）
    high_atr = support_resistance(sr_breach, 10.4, {}, atr=2.0)
    assert (high_atr["state"] == "逼近压力位"), f"state={high_atr['state']}, atr_band=4*2.0/1.0={8.0}"

    # 中等 ATR：atr=0.2，atr_band=0.8，upper=max(0.66,0.8)=0.8，lower=min(0.34,0.2)=0.2
    # price=10.4，pos=0.4 → 0.2 < 0.4 < 0.8 → 区间中枢震荡
    mid_atr = support_resistance(sr_breach, 10.4, {}, atr=0.2)
    assert (mid_atr["state"] == "区间中枢震荡"), f"state={mid_atr['state']}"

    # 高位价格 10.8，pos=0.8，在无 ATR 时刚好触发"运行于区间上半区"(>0.66)
    # 加 atr=0.2 后 upper=0.8，pos=0.8 不再 > upper → 改为"区间中枢震荡"
    high_price = support_resistance(sr_breach, 10.8, {}, atr=0.2)
    assert (high_price["state"] == "区间中枢震荡"), f"state={high_price['state']} (无ATR时会是上半区)"

    # 无 ATR 退化兼容
    no_atr_sr = support_resistance(sr_breach, 10.7, {}, atr=None)
    assert (no_atr_sr["state"] in ("区间中枢震荡", "运行于区间上半区")), f"state={no_atr_sr['state']}"

    # P2-7 数据源层：东财 kline_min override
    from backend.providers.eastmoney import EastmoneyProvider
    em = EastmoneyProvider()
    assert (hasattr(em, "kline_min") and callable(getattr(em, "kline_min", None))), "kline_min 方法存在"

    from backend.providers.base import NotSupported
    from backend.providers.sina import SinaProvider
    import asyncio as _asyncio
    sina = SinaProvider()
    try:
        await_result = _asyncio.run(sina.kline_min("600000", "SH", 24))
        assert (False), str(await_result)
    except NotSupported:
        assert (True), ""





def test_watch_monitor() -> None:
    add = service.watch_monitor({"status": "normal", "change_pct": 3.2, "volume_ratio": 1.8})
    reduce = service.watch_monitor({"status": "normal", "change_pct": -3.0, "volume_ratio": 1.0})
    observe = service.watch_monitor({"status": "normal", "change_pct": 1.2, "volume_ratio": 1.0})
    delayed = service.watch_monitor({"status": "delayed", "status_text": "数据更新延迟"})
    assert (add["action"] == "可加仓" and add["tone"] == "up"), str(add)
    assert (reduce["action"] == "应减仓" and reduce["tone"] == "down"), str(reduce)
    assert (observe["action"] == "继续观察"), str(observe)
    assert (delayed["action"] == "继续观察" and delayed["tone"] == "warn"), str(delayed)

    # ---- v3 扩展: 温和回调(浅跌 + 量能) / 放量上行(中涨 + 大量) ----
    mild_pullback = service.watch_monitor({"status": "normal", "change_pct": -2.0, "volume_ratio": 1.2})
    mild_pullback_no_vol = service.watch_monitor({"status": "normal", "change_pct": -2.0, "volume_ratio": 0.5})
    big_up_no_vol = service.watch_monitor({"status": "normal", "change_pct": 2.0, "volume_ratio": 1.5})
    big_up_strong_vol = service.watch_monitor({"status": "normal", "change_pct": 2.0, "volume_ratio": 2.5})
    mid_shrink = service.watch_monitor({"status": "normal", "change_pct": 1.5, "volume_ratio": 1.8})
    assert (mild_pullback["action"] == "温和回调" and mild_pullback["tone"] == "warn"), str(mild_pullback)
    assert (mild_pullback_no_vol["action"] != "温和回调"), str(mild_pullback_no_vol)
    assert (big_up_no_vol["action"] != "放量上行"), str(big_up_no_vol)
    assert (big_up_strong_vol["action"] == "放量上行" and big_up_strong_vol["tone"] == "up"), str(big_up_strong_vol)
    # 边界: change=1.0 / change=3.0
    edge_1 = service.watch_monitor({"status": "normal", "change_pct": 1.0, "volume_ratio": 2.5})
    edge_3 = service.watch_monitor({"status": "normal", "change_pct": 3.0, "volume_ratio": 2.5})
    assert (edge_1["action"] == "放量上行"), str(edge_1)
    assert (edge_3["action"] == "可加仓"), str(edge_3)
    # VR 边界 vr=2.0
    vr_low = service.watch_monitor({"status": "normal", "change_pct": 2.0, "volume_ratio": 1.99})
    vr_hi = service.watch_monitor({"status": "normal", "change_pct": 2.0, "volume_ratio": 2.0})
    assert (vr_low["action"] != "放量上行"), str(vr_low)
    assert (vr_hi["action"] == "放量上行"), str(vr_hi)
    assert (mid_shrink["action"] == "继续观察"), str(mid_shrink)

    # ---- v2: ATR 归一化 + 量比过滤 ----

    # 高波动股票（ATR=2 元，股价 20 元，ATR% = 10%）：涨 3% 仅 0.3 倍 ATR，
    # 不触发「可加仓」；涨 20% 才达到 2 倍 ATR，且量比达标
    high_vol = service.watch_monitor(
        {"status": "normal", "change_pct": 3.0, "volume_ratio": 2.0, "price": 20.0},
        atr=2.0,
    )
    assert (high_vol["action"] == "继续观察"), str(high_vol)

    # 用创业板代码让 change=16% 落在合法区间(<20% 涨停线)
    high_vol_strong = service.watch_monitor(
        {"status": "normal", "change_pct": 16.0, "volume_ratio": 2.0,
         "price": 20.0, "code": "300750", "market": "SZ"},
        atr=2.0,
    )
    assert (high_vol_strong["action"] == "可加仓" and high_vol_strong["tone"] == "up"), str(high_vol_strong)
    assert ("ATR" in high_vol_strong["reason"]), high_vol_strong["reason"]

    # 低波动股票（ATR=0.1 元，股价 20 元，ATR% = 0.5%）：涨 1% 即 2 倍 ATR → 加仓
    low_vol = service.watch_monitor(
        {"status": "normal", "change_pct": 1.0, "volume_ratio": 1.6, "price": 20.0},
        atr=0.1,
    )
    assert (low_vol["action"] == "可加仓" and low_vol["tone"] == "up"), str(low_vol)

    # 大涨无量：不触发加仓（无量拉升是出货形态）
    no_vol_add = service.watch_monitor(
        {"status": "normal", "change_pct": 6.0, "volume_ratio": 0.6, "price": 20.0},
        atr=0.1,
    )
    assert (no_vol_add["action"] == "继续观察"), str(no_vol_add)

    # ATR 不可用：退回到固定 3% 门槛（向后兼容）
    legacy_add = service.watch_monitor(
        {"status": "normal", "change_pct": 3.5, "volume_ratio": 1.6, "price": 20.0},
        atr=None,
    )
    assert (legacy_add["action"] == "可加仓" and "3.5" in legacy_add["reason"]), str(legacy_add)

    # 缩量阴跌：跌幅 -3.5% 但量比 0.5 → 不应触发减仓
    shrink_down = service.watch_monitor(
        {"status": "normal", "change_pct": -3.5, "volume_ratio": 0.5},
    )
    assert (shrink_down["action"] == "继续观察" and "地量阴跌" in shrink_down["reason"]), str(shrink_down)

    # 放量下跌：跌幅 -3.5% 且量比 1.2 → 应减仓
    vol_down = service.watch_monitor(
        {"status": "normal", "change_pct": -3.5, "volume_ratio": 1.2},
    )
    assert (vol_down["action"] == "应减仓" and vol_down["tone"] == "down"), str(vol_down)

    # 临界：跌幅 -3.0% 量比 0.79（边界以下）→ 不减仓
    edge_down = service.watch_monitor(
        {"status": "normal", "change_pct": -3.0, "volume_ratio": 0.79},
    )
    assert (edge_down["action"] == "继续观察"), str(edge_down)

    # 临界：跌幅 -3.0% 量比 0.80 → 触发减仓
    edge_down2 = service.watch_monitor(
        {"status": "normal", "change_pct": -3.0, "volume_ratio": 0.80},
    )
    assert (edge_down2["action"] == "应减仓"), str(edge_down2)

    # ---- v3: 涨跌停 / 异动放量 / 高换手 / 流动性低 / 谨慎持有 ----

    # 主板涨停(9.8%)：明确触碰涨停线
    main_limit_up = service.watch_monitor(
        {"status": "normal", "change_pct": 9.8, "volume_ratio": 2.0},
    )
    assert (main_limit_up["action"] == "涨停关注" and main_limit_up["tone"] == "warn"), str(main_limit_up)

    # 主板跌停(-9.8%)
    main_limit_down = service.watch_monitor(
        {"status": "normal", "change_pct": -9.8, "volume_ratio": 1.0},
    )
    assert (main_limit_down["action"] == "跌停风险" and main_limit_down["tone"] == "down"), str(main_limit_down)

    # 临界：主板 9.4%(< 9.7 抖动容忍) → 不应触发涨停
    just_under_limit = service.watch_monitor(
        {"status": "normal", "change_pct": 9.4, "volume_ratio": 2.0},
    )
    assert (just_under_limit["action"] != "涨停关注"), str(just_under_limit)

    # 创业板：15% 落在 20% 涨停线内，不应触发涨停
    gem_up = service.watch_monitor(
        {"status": "normal", "change_pct": 15.0, "volume_ratio": 2.0,
         "code": "300750", "market": "SZ"},
    )
    assert (gem_up["action"] != "涨停关注"), str(gem_up)

    # 创业板涨停(19.8%)
    gem_limit_up = service.watch_monitor(
        {"status": "normal", "change_pct": 19.8, "volume_ratio": 2.0,
         "code": "300750", "market": "SZ"},
    )
    assert (gem_limit_up["action"] == "涨停关注"), str(gem_limit_up)

    # ST 股票：4.8% 触及 5% 涨停线
    st_up = service.watch_monitor(
        {"status": "normal", "change_pct": 4.8, "volume_ratio": 1.0,
         "code": "600xxx", "name": "ST华联"},
    )
    assert (st_up["action"] == "涨停关注"), str(st_up)

    # ST 股票：4.5% 未触及 5% 涨停线
    st_under = service.watch_monitor(
        {"status": "normal", "change_pct": 4.5, "volume_ratio": 1.0,
         "name": "*ST华联"},
    )
    assert (st_under["action"] != "涨停关注"), str(st_under)

    # 异动放量：量比 3.5 但 change 仅 0.5% → 方向不明
    surge = service.watch_monitor(
        {"status": "normal", "change_pct": 0.5, "volume_ratio": 3.5},
    )
    assert (surge["action"] == "异动放量" and surge["tone"] == "warn"), str(surge)

    # 异动放量需方向不明：change 3% + 量比 3.5 → 走加仓判定而非异动放量
    surge_with_trend = service.watch_monitor(
        {"status": "normal", "change_pct": 3.0, "volume_ratio": 3.5},
    )
    assert (surge_with_trend["action"] != "异动放量"), str(surge_with_trend)

    # 高换手出货：换手 12% 且下跌
    active_sell = service.watch_monitor(
        {"status": "normal", "change_pct": -2.0, "volume_ratio": 1.0,
         "turnover": 12.0},
    )
    assert (active_sell["action"] == "高换手出货" and active_sell["tone"] == "down"), str(active_sell)

    # 高换手活跃：换手 12% 且上涨
    active_buy = service.watch_monitor(
        {"status": "normal", "change_pct": 2.0, "volume_ratio": 1.0,
         "turnover": 12.0},
    )
    assert (active_buy["action"] == "高换手活跃" and active_buy["tone"] == "warn"), str(active_buy)

    # 流动性低：换手 0.2%
    illiquid = service.watch_monitor(
        {"status": "normal", "change_pct": 0.0, "volume_ratio": 0.5,
         "turnover": 0.2},
    )
    assert (illiquid["action"] == "流动性低" and illiquid["tone"] == "warn"), str(illiquid)

    # 流动性低优先级低于高换手：换手 5% 不触发流动性低
    normal_to = service.watch_monitor(
        {"status": "normal", "change_pct": 0.0, "volume_ratio": 0.5,
         "turnover": 5.0},
    )
    assert (normal_to["action"] != "流动性低"), str(normal_to)

    # 谨慎持有：跌幅 -2% 且量比 0.5(未触发减仓)
    weak_hold = service.watch_monitor(
        {"status": "normal", "change_pct": -2.0, "volume_ratio": 0.5},
    )
    assert (weak_hold["action"] == "谨慎持有" and weak_hold["tone"] == "flat"), str(weak_hold)

    # 谨慎持有优先级低于异动放量：量比 3.5 + 跌 -2% 走异动放量
    weak_surge = service.watch_monitor(
        {"status": "normal", "change_pct": -1.8, "volume_ratio": 3.5},
    )
    assert (weak_surge["action"] == "异动放量"), str(weak_surge)

    # ATR 数据异常：price=0 时归一化不生效，退回到固定门槛
    atr_no_price = service.watch_monitor(
        {"status": "normal", "change_pct": 3.5, "volume_ratio": 1.6, "price": 0},
        atr=0.5,
    )
    assert (atr_no_price["action"] == "可加仓" and "ATR" not in atr_no_price["reason"]), str(atr_no_price)

    # ---- v4: 资金流信号(摘要为 indicators.summarize_flow 的输出结构) ----

    def _flow(state, main_last, streak=0, streak_dir="", grade="", main_last5=0.0, fresh=True):
        return {
            "available": True,
            "state": state,
            "state_grade": grade or ("inflow" if main_last > 0 else ("outflow" if main_last < 0 else "neutral")),
            "main_last": main_last,
            "streak": streak,
            "streak_dir": streak_dir,
            "main_last5": main_last5,
            "fresh": fresh,
            "last_date": "2026-08-28",
        }

    # 量价背离1: 价↑ 但主力净流出 > 50 万
    div_up = service.watch_monitor(
        {"status": "normal", "change_pct": 1.5, "volume_ratio": 1.0},
        flow=_flow("主力净流出（当日）", -8e6),
    )
    assert (div_up["action"] == "量价背离" and div_up["tone"] == "warn"), str(div_up)

    # 量价背离2: 价↓ 0.5%(< 1%)但主力净流入 > 50 万。
    # 注:价跌幅必须 < 1%,否则会被「主力护盘」(change ≤ -1% + 资金净流入)抢先命中。
    div_down = service.watch_monitor(
        {"status": "normal", "change_pct": -0.5, "volume_ratio": 1.0},
        flow=_flow("主力净流入（当日）", 8e6),
    )
    assert (div_down["action"] == "量价背离" and "洗盘" in div_down["reason"]), str(div_down)

    # 量价背离死区: 价微涨 0.3% 但资金流出 → 不触发背离
    no_div = service.watch_monitor(
        {"status": "normal", "change_pct": 0.3, "volume_ratio": 1.0},
        flow=_flow("主力净流出（当日）", -1e7),
    )
    assert (no_div["action"] != "量价背离"), str(no_div)

    # 主力抢筹 + 价涨 → 主力抢筹
    rally_up = service.watch_monitor(
        {"status": "normal", "change_pct": 1.0, "volume_ratio": 1.0},
        flow=_flow("主力抢筹（当日）", 5e6, streak=3, streak_dir="流入"),
    )
    assert (rally_up["action"] == "主力抢筹" and rally_up["tone"] == "up"), str(rally_up)

    # 主力抢筹 + 价跌 → 仍报主力抢筹(强调资金意图,提示后续可能反弹)
    rally_down = service.watch_monitor(
        {"status": "normal", "change_pct": -2.0, "volume_ratio": 1.0},
        flow=_flow("主力抢筹（当日）", 5e6, streak=3, streak_dir="流入"),
    )
    assert (rally_down["action"] == "主力抢筹" and "承压" in rally_down["reason"]), str(rally_down)

    # 主力出逃 + 价跌 → 主力出货
    dump_down = service.watch_monitor(
        {"status": "normal", "change_pct": -1.0, "volume_ratio": 1.0},
        flow=_flow("主力出逃（当日）", -5e6, streak=3, streak_dir="流出"),
    )
    assert (dump_down["action"] == "主力出货" and dump_down["tone"] == "down"), str(dump_down)

    # 主力出逃 + 价涨 → 警惕诱多
    dump_up = service.watch_monitor(
        {"status": "normal", "change_pct": 2.0, "volume_ratio": 1.0},
        flow=_flow("主力出逃（当日）", -5e6, streak=3, streak_dir="流出"),
    )
    assert (dump_up["action"] == "主力出货" and "诱多" in dump_up["reason"]), str(dump_up)

    # 主力护盘: 价跌 ≥ 1% 但当日资金净流入
    support = service.watch_monitor(
        {"status": "normal", "change_pct": -1.5, "volume_ratio": 1.0},
        flow=_flow("主力净流入（当日）", 3e6),
    )
    assert (support["action"] == "主力护盘" and support["tone"] == "up"), str(support)

    # 持续流入: 连 3 日「主力净流入」(非抢筹/出逃中间档)
    persist_in = service.watch_monitor(
        {"status": "normal", "change_pct": 0.5, "volume_ratio": 0.8},
        flow=_flow("主力净流入（当日）", 1e6, streak=5, streak_dir="流入",
                   grade="inflow", main_last5=8e6),
    )
    assert (persist_in["action"] == "持续流入" and persist_in["tone"] == "up"), str(persist_in)

    # 持续流出: 连 3 日「主力净流出」
    persist_out = service.watch_monitor(
        {"status": "normal", "change_pct": -0.5, "volume_ratio": 0.8},
        flow=_flow("主力净流出（当日）", -1e6, streak=4, streak_dir="流出",
                   grade="outflow", main_last5=-6e6),
    )
    assert (persist_out["action"] == "持续流出" and persist_out["tone"] == "down"), str(persist_out)

    # 资金流信号优先级高于量比: change=3% + 主力抢筹 → 应走资金流分支(主力抢筹)而非可加仓
    priority = service.watch_monitor(
        {"status": "normal", "change_pct": 3.0, "volume_ratio": 2.0},
        flow=_flow("主力抢筹（当日）", 5e6, streak=3, streak_dir="流入"),
    )
    assert (priority["action"] == "主力抢筹"), str(priority)

    # 涨跌停优先于资金流: 涨停 + 主力出货 → 涨停关注(硬规则)
    limit_priority = service.watch_monitor(
        {"status": "normal", "change_pct": 9.8, "volume_ratio": 2.0},
        flow=_flow("主力出货（当日）", -5e6, streak=3, streak_dir="流出"),
    )
    assert (limit_priority["action"] == "涨停关注"), str(limit_priority)

    # 资金流不可用: flow=None → 走原逻辑(向后兼容)
    no_flow = service.watch_monitor(
        {"status": "normal", "change_pct": 3.0, "volume_ratio": 1.8},
    )
    assert (no_flow["action"] == "可加仓"), str(no_flow)

    # 资金流不可用2: available=False → 同样跳过资金流
    no_avail = service.watch_monitor(
        {"status": "normal", "change_pct": 3.0, "volume_ratio": 1.8},
        flow={"available": False},
    )
    assert (no_avail["action"] == "可加仓"), str(no_avail)

    # 资金流非当日: fresh=False → reason 带真实日期标注（main_last 是单日口径，
    # 旧文案「（近5日）」误标，v5 改为「截至MM-DD」与两融风格一致）
    stale = service.watch_monitor(
        {"status": "normal", "change_pct": 1.5, "volume_ratio": 1.0},
        flow=_flow("主力抢筹", 5e6, streak=3, streak_dir="流入", fresh=False),
    )
    assert (stale["action"] == "主力抢筹" and "截至08-28" in stale["reason"]), str(stale)

    old_trading, old_session = service.is_trading_now, service.session_state
    try:
        service.is_trading_now = lambda: True
        service.session_state = lambda: "open"
        assert (service.session_info()["interval_ms"] == 5000)
    finally:
        service.is_trading_now, service.session_state = old_trading, old_session




def test_intraday_consistency_neutral_for_choppy() -> None:
    """横盘对任何日线趋势都是中性：空头+震荡 / 多头+震荡 / 震荡+涨跌 不判背离。

    2026-09-09 用户核查发现：601179 日线空头 + 60分线震荡被判「盘中日线趋势
    分歧」warn 横幅，把常态当风险警示。只有方向相反（空头+上涨 / 多头+下跌）
    才是真背离。
    """
    from backend.indicators import intraday_consistency

    # 中性组合 → aligned=None，不出横幅
    assert intraday_consistency("空头趋势", {"available": True, "label": "震荡", "chg": 0.48})["aligned"] is None
    assert intraday_consistency("多头趋势", {"available": True, "label": "震荡", "chg": -0.2})["aligned"] is None
    assert intraday_consistency("震荡整理", {"available": True, "label": "上涨", "chg": 1.0})["aligned"] is None
    assert intraday_consistency("空头趋势", {"available": True, "label": "震荡", "chg": 0.5})["hint"] == ""
    # 真背离：方向相反
    assert intraday_consistency("空头趋势", {"available": True, "label": "上涨", "chg": 1.2})["aligned"] is False
    assert intraday_consistency("多头趋势", {"available": True, "label": "下跌", "chg": -1.0})["aligned"] is False
    # 同向：一致
    assert intraday_consistency("空头趋势", {"available": True, "label": "下跌", "chg": -1.0})["aligned"] is True


def test_intraday_state_amp_gate() -> None:
    """盘口位置标签需要振幅 ≥3%：小振幅时 pos 分母过小是纯噪声。

    2026-09-09 核查：601179 振幅 1.9% 被标「高位强势（88%）」与空头趋势并存。
    """
    from types import SimpleNamespace
    from backend.indicators import intraday_state_from_quote

    # 振幅 1.9%、pos 88% → 不出「高位强势」
    q_small = SimpleNamespace(price=12.57, prev_close=12.42, high=12.60, low=12.36,
                              change_pct=1.21, volume_ratio=None)
    out = intraday_state_from_quote(q_small)
    assert "高位强势" not in out["label"], out
    # 同样 pos 但振幅 6% → 「高位强势」成立
    q_big = SimpleNamespace(price=13.00, prev_close=12.42, high=13.16, low=12.42,
                            change_pct=4.67, volume_ratio=None)
    out_big = intraday_state_from_quote(q_big)
    assert "高位强势" in out_big["label"], out_big
    # 量比信号不受振幅门槛影响
    q_vr = SimpleNamespace(price=12.57, prev_close=12.42, high=12.60, low=12.36,
                           change_pct=1.21, volume_ratio=2.5)
    out_vr = intraday_state_from_quote(q_vr)
    assert "放量上攻" in out_vr["label"], out_vr


def test_summarize_flow_fresh_uses_real_today() -> None:
    """fresh 参照必须是「今天真实日期」而非 K 线最新日期。

    2026-09-09 核查：同花顺日线盘中不含当日 K（最新 09-08），flow 也到 09-08，
    旧调用把 K 线日期当 ref_date → 昨日资金流被判「当日」。
    """
    from backend.indicators import summarize_flow
    from backend.providers.base import FlowDay

    rows = [
        FlowDay(date="2026-09-04", main=1.0e8, sm=0, md=0, lg=0, xl=0.5e8),
        FlowDay(date="2026-09-08", main=-1.44e8, sm=0, md=0, lg=0, xl=-0.8e8),
    ]
    # 错误用法（旧代码）：ref_date=K线最新日期 09-08 → 昨日被当「当日」
    wrong = summarize_flow(rows, ref_date="2026-09-08")
    assert wrong["fresh"] is True
    # 正确用法：ref_date=今天（09-09 盘中）→ flow 未含今日行 → fresh=False 降级近5日
    right = summarize_flow(rows, ref_date="2026-09-09")
    assert right["fresh"] is False, right["fresh"]
    assert right["state"] == "主力净流出（近5日）", right["state"]
    assert right["last_date"] == "2026-09-08"
    # 收盘后 flow 更新今日行 → fresh=True
    rows_today = rows + [FlowDay(date="2026-09-09", main=2.0e8, sm=0, md=0, lg=0, xl=1.2e8)]
    after = summarize_flow(rows_today, ref_date="2026-09-09")
    assert after["fresh"] is True and after["state"] == "主力净流入（当日）", after["state"]


def test_watch_monitor_v5(monkeypatch) -> None:
    """v5：涨停融合信号（连板+资金流）与 VWAP 分时位置信号。"""
    # VWAP 分时信号仅盘中生效（v5.1：收盘后快照冻结会误导，见 601086 案例）
    monkeypatch.setattr(service, "is_trading_now", lambda: True)
    # 连板股涨停 → 「连板涨停」up，reason 带连板数
    lianban = service.watch_monitor(
        {"status": "normal", "change_pct": 10.02, "market": "SZ", "code": "000523"},
        zt={"lianban": 2, "label": "2连板"},
    )
    assert (lianban["action"] == "连板涨停" and lianban["tone"] == "up"), str(lianban)
    assert "2连板" in lianban["reason"], lianban["reason"]

    # 涨停 + 主力大幅流出 → reason 警示开板分歧
    out_flow = service.watch_monitor(
        {"status": "normal", "change_pct": 9.98, "market": "SH", "code": "600000"},
        flow={"available": True, "main_last": -5e7, "fresh": True, "state": "主力净流出"},
    )
    assert (out_flow["action"] == "涨停关注" and "开板分歧" in out_flow["reason"]), str(out_flow)

    # 涨停 + 主力大幅流入 → reason 提示资金接力
    in_flow = service.watch_monitor(
        {"status": "normal", "change_pct": 9.98, "market": "SH", "code": "600000"},
        flow={"available": True, "main_last": 8e7, "fresh": True, "state": "主力净流入"},
    )
    assert "资金接力" in in_flow["reason"], str(in_flow)

    # 涨停但无资金流/连板数据 → 退化为「涨停关注」（向后兼容）
    plain = service.watch_monitor(
        {"status": "normal", "change_pct": 10.02, "market": "SH", "code": "600000"},
    )
    assert (plain["action"] == "涨停关注" and plain["tone"] == "warn"), str(plain)

    # 涨但价在分时均价线下方 1.5%+ → 「冲高回落」warn
    fade = service.watch_monitor(
        {"status": "normal", "change_pct": 2.5, "volume_ratio": 1.0,
         "vwap": 10.5, "deviation_pct": -2.1},
    )
    assert (fade["action"] == "冲高回落" and fade["tone"] == "warn"), str(fade)

    # 跌但站回分时均价线上方 → 「探底回升」up
    recover = service.watch_monitor(
        {"status": "normal", "change_pct": -1.8, "volume_ratio": 1.0,
         "vwap": 10.0, "deviation_pct": 2.0},
    )
    assert (recover["action"] == "探底回升" and recover["tone"] == "up"), str(recover)

    # 偏离在 ±1.5% 死区内 → 不触发 VWAP 信号，回落到原逻辑
    neutral = service.watch_monitor(
        {"status": "normal", "change_pct": 1.2, "volume_ratio": 1.0,
         "vwap": 10.0, "deviation_pct": 0.5},
    )
    assert neutral["action"] == "继续观察", str(neutral)

    # 无 vwap 数据 → 完全跳过 VWAP 分支（向后兼容）
    no_vwap = service.watch_monitor(
        {"status": "normal", "change_pct": 2.5, "volume_ratio": 1.0},
    )
    assert no_vwap["action"] == "继续观察", str(no_vwap)

    # v5.1：收盘后（is_trading_now=False）VWAP 分时信号不再输出——
    # 尾盘脉冲会把「高开跳水→弱势震荡→尾盘偷袭」的票定格成「探底回升」
    monkeypatch.setattr(service, "is_trading_now", lambda: False)
    closed = service.watch_monitor(
        {"status": "normal", "change_pct": -0.47, "volume_ratio": 1.22,
         "vwap": 14.4975, "deviation_pct": 2.638},
    )
    assert closed["action"] != "探底回升", str(closed)


def test_tail_rush_pct_formats() -> None:
    """尾盘拉升幅度：兼容 tencent（202609091500）与 eastmoney（2026-09-09 15:00）两种日期格式。"""
    from backend.service import _tail_rush_pct
    from backend.providers.base import Bar

    def bars_tencent():
        return [
            Bar(date="202609091400", open=14.5, close=14.5, high=14.5, low=14.5),
            Bar(date="202609091430", open=14.5, close=14.52, high=14.55, low=14.48),
            Bar(date="202609091435", open=14.6, close=14.70, high=14.72, low=14.58),
            Bar(date="202609091500", open=14.8, close=14.88, high=14.90, low=14.79),
        ]

    def bars_eastmoney():
        return [
            Bar(date="2026-09-09 14:00", open=14.5, close=14.5, high=14.5, low=14.5),
            Bar(date="2026-09-09 14:30", open=14.5, close=14.52, high=14.55, low=14.48),
            Bar(date="2026-09-09 14:35", open=14.6, close=14.70, high=14.72, low=14.58),
            Bar(date="2026-09-09 15:00", open=14.8, close=14.88, high=14.90, low=14.79),
        ]

    # 基准 = 14:30 收盘 14.52 → 现价 14.88 = +2.48%
    for bars in (bars_tencent(), bars_eastmoney()):
        r = _tail_rush_pct(bars, today="20260909")
        assert r is not None and abs(r - 2.48) < 0.01, r
    # 无今日数据 → None；只有 14:30 前数据 → 拉升 0.0（未开盘尾盘窗口）
    assert _tail_rush_pct([], today="20260909") is None
    assert _tail_rush_pct([bars_tencent()[0]], today="20260909") == 0.0
    # 今日缺早盘（第一根就是 14:35）→ 无 14:30 基准 → None
    late_only = [Bar(date="202609091435", open=14.6, close=14.7, high=14.72, low=14.58)]
    assert _tail_rush_pct(late_only, today="20260909") is None


def test_watch_monitor_tail_rush(monkeypatch) -> None:
    """v5.2 尾盘偷袭：仅盘中 14:30 后且拉升 ≥2% 触发，优先于 VWAP。"""
    monkeypatch.setattr(service, "is_trading_now", lambda: True)

    # 尾盘 14:31 + 拉升 2.5% → 触发
    monkeypatch.setattr(service, "_now_hhmm", lambda: "1431")
    hit = service.watch_monitor(
        {"status": "normal", "change_pct": 1.0, "volume_ratio": 1.0,
         "vwap": 10.0, "deviation_pct": 2.0},
        tail_rush=2.5,
    )
    assert (hit["action"] == "尾盘偷袭" and hit["tone"] == "warn"), str(hit)
    assert "14:30" in hit["reason"], hit["reason"]

    # 拉升不足 2% → 不触发
    small = service.watch_monitor(
        {"status": "normal", "change_pct": 1.0, "volume_ratio": 1.0},
        tail_rush=1.2,
    )
    assert small["action"] != "尾盘偷袭", str(small)

    # 14:30 前（时间窗未开）→ 不触发
    monkeypatch.setattr(service, "_now_hhmm", lambda: "1400")
    early = service.watch_monitor(
        {"status": "normal", "change_pct": 1.0, "volume_ratio": 1.0},
        tail_rush=3.0,
    )
    assert early["action"] != "尾盘偷袭", str(early)

    # 收盘后 → 不触发（is_trading_now=False）
    monkeypatch.setattr(service, "_now_hhmm", lambda: "1431")
    monkeypatch.setattr(service, "is_trading_now", lambda: False)
    closed = service.watch_monitor(
        {"status": "normal", "change_pct": 1.0, "volume_ratio": 1.0},
        tail_rush=2.5,
    )
    assert closed["action"] != "尾盘偷袭", str(closed)
