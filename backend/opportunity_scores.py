"""机会投资评分引擎（从 opportunity_screener.py 拆出，体检 M7）。

六层漏斗的第 4~6 层 + 交易计划：妖股基因 / 资金持续性 / 分歧转强 /
分时攻击 / 概率模型 / 准入排除 / 风险收益比。仅 registry（_fetch_min5
5分钟K）与 utils 依赖，函数名与拆分前完全一致，opportunity_screener
统一 re-export。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from .providers import registry
from .utils import is_trading_now, to_float

log = logging.getLogger("providers.registry")

# 主板/创业科创涨停幅度（妖股基因统计口径）
_ZT_PCT_MAIN = 9.8
_ZT_PCT_20 = 19.8

# ------------------------------------------------------------------ 第四层-①：妖股基因 100

def _stage_of(bstats: dict[str, Any] | None) -> tuple[str, float]:
    """板块阶段与周期分（无数据 → 退潮保守处理：禁止准入）。"""
    if not bstats:
        return "未知", 0.0
    return bstats.get("stage") or "未知", float(bstats.get("stage_score") or 0.0)


def _count_zt(bars: list[dict[str, Any]], days: int) -> tuple[int, int]:
    """近 N 日涨停次数与最大连板数（主板 9.8%/20cm 板 19.8% 阈值）。"""
    recent = bars[-days:]
    zt_times, run, best = 0, 0, 0
    for b in recent:
        c = b.get("change_pct")
        if c is None:
            continue
        code_prefix = str(b.get("code") or "")
        thr = _ZT_PCT_20 if code_prefix.startswith(("30", "68")) else _ZT_PCT_MAIN
        if c >= thr:
            zt_times += 1
            run += 1
            best = max(best, run)
        else:
            run = 0
    return zt_times, best


def _yaogu_score(profile: dict[str, Any], bstats: dict[str, Any],
                 bars: list[dict[str, Any]]) -> dict[str, Any]:
    """妖股基因 100 分（需求第五节 10 项）。全部为可计算口径，无编造项。"""
    items: list[dict[str, Any]] = []
    zt120, best_lb_120 = _count_zt(bars, 120)
    zt60, _ = _count_zt(bars, 60)
    float_mv = profile.get("float_mv")
    price = profile.get("price")
    turnover = profile.get("turnover")

    def _add(name: str, got: float, mx: int, note: str = "") -> None:
        items.append({"name": name, "got": round(got, 1), "max": mx, "note": note})

    # ① 历史爆发 10
    s = 4 if zt120 >= 1 else 1
    if zt120 >= 5: s = 10
    elif zt120 >= 3: s = 7
    if best_lb_120 >= 3: s = min(10, s + 2)
    _add("历史爆发能力", s, 10, f"120日涨停{zt120}次/最大连板{best_lb_120}")
    # ② 市值弹性 10
    if float_mv is None:
        s2, note = 3, "流通市值【数据缺失】"
    elif float_mv < 100: s2, note = 10, f"流通{float_mv:.0f}亿，弹性极大(风险同步+)"
    elif float_mv < 200: s2, note = 9, f"流通{float_mv:.0f}亿，弹性优秀"
    elif float_mv < 300: s2, note = 7, f"流通{float_mv:.0f}亿，弹性可"
    elif float_mv < 500: s2, note = 4, f"流通{float_mv:.0f}亿，偏大"
    else: s2, note = 2, f"流通{float_mv:.0f}亿，巨象"
    _add("市值弹性", s2, 10, note)
    # ③ 股价弹性 8（非硬性淘汰）
    if price is None:
        s3, note = 3, "价格【数据缺失】"
    elif price < 20: s3, note = 8, f"低价{price:.1f}元"
    elif price < 50: s3, note = 7, f"{price:.1f}元"
    elif price < 80: s3, note = 6, f"{price:.1f}元"
    elif price < 120: s3, note = 3, f"中高价{price:.1f}元"
    else: s3, note = 2, f"高价{price:.1f}元"
    _add("股价弹性", s3, 8, note)
    # ④ 换手能力 10（5%~20% 优秀）
    if turnover is None:
        s4, note = 3, "换手【数据缺失】"
    elif 7 <= turnover <= 15: s4, note = 10, f"换手{turnover:.1f}%，最佳区间"
    elif 5 <= turnover <= 20: s4, note = 8, f"换手{turnover:.1f}%"
    elif turnover > 25: s4, note = 3, f"换手{turnover:.1f}%，过热"
    elif turnover < 5: s4, note = 2, f"换手{turnover:.1f}%，不足"
    else: s4, note = 6, f"换手{turnover:.1f}%"
    _add("换手能力", s4, 10, note)
    # ⑤ 涨停/大阳基因 12（近 60 日）
    big_yang = sum(1 for b in bars[-60:] if (b.get("change_pct") or 0) >= 7)
    s5 = 2 + (8 if zt60 >= 3 else 5 if zt60 >= 1 else 0) + (2 if big_yang >= 3 else 0)
    _add("涨停/大阳基因", min(12, s5), 12, f"60日涨停{zt60}次/大阳{big_yang}次")
    # ⑥ 资金攻击性 15
    main_in = profile.get("main_net_inflow") or 0
    main_pct = profile.get("main_net_pct")
    s6 = 0
    if main_in >= 5e7: s6 += 8
    elif main_in >= 2e7: s6 += 6
    elif main_in >= 1e7: s6 += 4
    elif main_in > 0: s6 += 2
    if main_pct is not None:
        s6 += 4 if main_pct >= 10 else 2 if main_pct >= 5 else 0
    _add("资金攻击性", s6, 15 if main_pct is not None else 11,
         f"主力净流入{main_in/1e4:.0f}万/净比{main_pct if main_pct is not None else '缺失'}")
    # ⑦ 题材辨识度 10（板块是否处于当日强势榜）
    rank = bstats.get("hot_rank")
    s7 = 10 if (rank is not None and rank < 3) else 7 if (rank is not None and rank < 8) else 3
    _add("题材辨识度", s7, 10, "当日核心热点" if s7 >= 7 else "非核心热点/冷门")
    # ⑧ 龙头/核心地位 10
    lb = profile.get("lianban") or 0
    is_top = bstats.get("max_lianban") == lb and lb >= 1
    if is_top: s8, note = 10, "板块最高连板（龙头）"
    elif lb >= 2: s8, note = 7, f"{lb}连板（中军/核心）"
    elif lb == 1: s8, note = 5, "首板"
    else: s8, note = 3, "非涨停（热门榜入选）"
    _add("龙头/核心地位", s8, 10, note)
    # ⑨ 股性 5（120日日均振幅 + 涨停频率）
    amps = [((b.get("high") or 0) / (b.get("low") or 1) - 1) * 100
            for b in bars[-60:] if b.get("high") and b.get("low")]
    avg_amp = sum(amps) / len(amps) if amps else None
    s9 = (3 if avg_amp is not None and avg_amp >= 5 else 2 if avg_amp else 1) + (2 if zt120 >= 3 else 0)
    _add("股性", s9, 5, f"60日日均振幅{avg_amp:.1f}%" if avg_amp is not None else "振幅【数据缺失】")
    # ⑩ 分歧转强历史 10：跌≥2% 次日反包收阳≥3%
    rebound = 0
    for i in range(1, min(60, len(bars))):
        prev, cur = bars[-i - 1], bars[-i]
        if (prev.get("change_pct") or 0) <= -2 and (cur.get("change_pct") or 0) >= 3:
            rebound += 1
    s10 = 8 if rebound >= 2 else 5 if rebound == 1 else 2
    _add("分歧转强历史", s10, 10, f"60日跌后反包{rebound}次")
    total = round(sum(i["got"] for i in items), 1)
    return {"score": min(100.0, total), "items": items, "missing": []}


# ------------------------------------------------------------------ 第四层-②：资金持续性 100

def _fund_score(profile: dict[str, Any], bars: list[dict[str, Any]],
                board_cand_count: int) -> dict[str, Any]:
    """资金持续性 100 分（需求第七节 10 项）。flow 30 日 + 实时主力 + 量能对比。"""
    flow = profile.get("flow") or []
    items: list[dict[str, Any]] = []
    missing: list[str] = []

    def _add(name: str, got: float, mx: float, note: str = "") -> None:
        items.append({"name": name, "got": round(got, 1), "max": mx, "note": note})

    q_main = profile.get("main_net_inflow")
    q_pct = profile.get("main_net_pct")
    # ① 当日主力净流入 20（实时快照；缺失退回 flow 最后一日）
    if q_main is not None:
        s = 20 if q_main >= 1e8 else 16 if q_main >= 5e7 else 12 if q_main >= 3e7 else 8 if q_main > 0 else 0
        _add("当日主力净流入", s, 20, f"{q_main/1e8:.2f}亿")
    elif flow:
        q_main = flow[-1].get("main") or 0
        s = 16 if q_main >= 1e8 else 12 if q_main >= 5e7 else 8 if q_main > 0 else 0
        _add("当日主力净流入", s, 20, f"{q_main/1e8:.2f}亿(日级源)")
    else:
        q_main = 0
        _add("当日主力净流入", 0, 20, "【数据缺失】")
        missing.append("当日主力资金")
    # ② 主力净比 10
    if q_pct is not None:
        _add("主力净比", 10 if q_pct >= 15 else 8 if q_pct >= 8 else 5 if q_pct >= 3 else 2,
             10, f"{q_pct:.1f}%")
    else:
        _add("主力净比", 2, 10, "【数据缺失】")
        missing.append("主力净比")
    # ③④ 超大单/大单（日级，最新一日）
    xl_today: float | None = None
    lg_today: float | None = None
    if flow:
        xl0 = flow[-1].get("xl") or 0
        lg0 = flow[-1].get("lg") or 0
        xl_today, lg_today = xl0, lg0
        _add("超大单净流入", 15 if xl0 >= 5e7 else 11 if xl0 >= 2e7 else 6 if xl0 > 0 else 0,
             15, f"{xl0/1e8:.2f}亿")
        _add("大单净流入", 10 if lg0 >= 3e7 else 7 if lg0 >= 1e7 else 4 if lg0 > 0 else 0,
             10, f"{lg0/1e8:.2f}亿")
    else:
        _add("超大单净流入", 0, 15, "【数据缺失】")
        _add("大单净流入", 0, 10, "【数据缺失】")
        missing.extend(["超大单", "大单"])
    # ⑤⑥⑦ 3/5/30 日趋势
    day3: float | None = None
    day5: float | None = None
    day30: float | None = None
    if len(flow) >= 6:
        s3 = sum((d.get("main") or 0) for d in flow[-3:])
        s5 = sum((d.get("main") or 0) for d in flow[-5:])
        s30 = sum((d.get("main") or 0) for d in flow)
        day3, day5, day30 = s3, s5, s30
        consec = sum(1 for d in flow[-3:] if (d.get("main") or 0) > 0)
        base = 15 if s3 >= 1.5e8 else 11 if s3 >= 5e7 else 7 if s3 > 0 else 0
        _add("近3日资金趋势", min(15, base + (2 if consec == 3 else 0)), 15,
             f"3日{s3/1e8:+.2f}亿" + (f"，连续{consec}日流入" if consec else ""))
        _add("近5日资金趋势", 10 if s5 >= 5e7 else 6 if s5 > 0 else 0, 10, f"5日{s5/1e8:+.2f}亿")
        _add("30日资金趋势", 5 if s30 > 0 else 0, 5, f"30日{s30/1e8:+.2f}亿")
        # 策略降分：当日大涨大流入但近5日持续流出 → ×0.8
        if s5 < -5e6 and q_main and q_main > 3e7:
            for it in items:
                it["got"] = round(it["got"] * 0.8, 1)
            items[-3]["note"] += "；⚠近5日流出，持续性打折"
    else:
        _add("近3日资金趋势", 0, 15, "【数据缺失】")
        _add("近5日资金趋势", 0, 10, "【数据缺失】")
        _add("30日资金趋势", 0, 5, "【数据缺失】")
        missing.append("3/5/30日资金流")
    # ⑧ 成交额增长 5（当日 vs 5日均额）
    amt = profile.get("amount")
    avg_amt = None
    if len(bars) >= 6:
        avg_amt = sum((b.get("amount") or 0) for b in bars[-6:-1]) / 5 or None
    if amt and avg_amt:
        ratio = amt / avg_amt
        _add("成交额增长", 5 if ratio >= 1.5 else 3 if ratio >= 1.2 else 1, 5, f"较5日均量{ratio:.1f}倍")
    else:
        _add("成交额增长", 0, 5, "【数据缺失】")
    # ⑨ 换手与资金匹配 5
    to = profile.get("turnover")
    _add("换手资金匹配", 5 if (to and 7 <= to <= 20 and (q_main or 0) > 0) else 2, 5,
         f"换手{to:.1f}%" if to else "")
    # ⑩ 板块资金共振 5（代理：同板块候选当日主力为正的家数）
    _add("板块资金共振", 5 if board_cand_count >= 2 else 2 if board_cand_count == 1 else 0,
         5, f"同板块{board_cand_count}只候选主力净流入" if board_cand_count else "板块资金【数据缺失·代理】")
    total = round(sum(i["got"] for i in items), 1)
    return {"score": min(100.0, total), "items": items, "missing": missing,
            "day": q_main, "day3": day3, "day5": day5, "day30": day30,
            "xl_today": xl_today, "lg_today": lg_today}


# ------------------------------------------------------------------ 第四层-③：分歧转强 & 第四层-④：分时攻击（5 分钟级）

async def _fetch_min5(code: str, market: str) -> list[dict[str, Any]]:
    """今日 5 分钟 K 线（provider 原生 klt=5），进程内 TTL 缓存。

    失败/为空返回 [] → 分歧转强与分时攻击标【数据缺失】→ 个股门槛不通过。
    """
    key = f"{code}.{market}"
    hit = _min5_cache.get(key)
    ttl = 120.0 if is_trading_now() else 900.0
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        # registry().kline_min 返回 (bars, 源名) 元组——直接迭代会在第二个元素
        # (str) 上抛 AttributeError 被吞掉，导致分时数据 100% 缺失、composite
        # 恒丢 35 分，页面永远空仓（2026-09-09 实弹确诊的历史性 Bug）
        bars, _src = await registry().kline_min(code, market, 48, klt=5)
        rows = [{"date": b.date, "open": b.open, "close": b.close, "high": b.high,
                 "low": b.low, "volume": b.volume, "amount": b.amount}
                for b in bars]
        today = datetime.now().strftime("%Y-%m-%d")
        rows = [r for r in rows if str(r["date"]).startswith(today)] or rows[-48:]
    except Exception as exc:  # noqa: BLE001
        log.debug("5分钟K线 %s 失败: %s", key, exc)
        rows = []
    _min5_cache[key] = (time.time(), rows)
    return rows


_min5_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _intraday_features(min5: list[dict[str, Any]],
                       profile: dict[str, Any]) -> dict[str, Any] | None:
    """从 5 分钟 K 线提取分时特征。数据不足返回 None。"""
    if len(min5) < 8:
        return None
    opens = min5[0]["open"]
    highs = [b["high"] for b in min5 if b.get("high")]
    lows = [b["low"] for b in min5 if b.get("low")]
    vols = [b.get("volume") or 0 for b in min5]
    closes = [b["close"] for b in min5]
    day_high = max(highs) if highs else None
    # 早盘（前 8 根 ≈ 前 40 分钟）冲高与随后的回落
    early = min5[:8]
    early_high = max((b["high"] for b in early if b.get("high")), default=None)
    early_vol = sum(vols[:8]) or 1
    after_high_idx = next((i for i, b in enumerate(min5) if b.get("high") == day_high), 0)
    pullback_low = min((b["low"] for b in min5[after_high_idx:] if b.get("low")), default=None)
    pullback_vol = sum(vols[after_high_idx:]) / max(1, len(vols) - after_high_idx)
    last_close = closes[-1]
    vwap = profile.get("vwap")
    prev_close = profile.get("prev_close")
    feat = {
        "open": opens, "day_high": day_high, "early_high": early_high,
        "pullback_low": pullback_low, "pullback_depth":
            (1 - pullback_low / early_high) * 100 if (early_high and pullback_low) else None,
        "pullback_vol_ratio": pullback_vol / early_vol,
        "price": profile.get("price"), "vwap": vwap, "prev_close": prev_close,
        "above_vwap": (last_close >= vwap) if (vwap and last_close) else None,
        "break_early_high": (last_close >= early_high * 0.995) if early_high else None,
        "no_new_high_after_surge": (
            after_high_idx < len(min5) - 3 and day_high and last_close < day_high * 0.985),
        "open_gap": ((opens / prev_close) - 1) * 100 if (opens and prev_close) else None,
        "after_high_idx": after_high_idx,
        "n_bars": len(min5),
    }
    return feat


def _minute_score(profile: dict[str, Any], min5: list[dict[str, Any]],
                  bstats: dict[str, Any]) -> dict[str, Any] | None:
    """分时攻击结构 A/B/C/D + 100 分制（需求第九/十节）。

    盘口五档主动买盘无数据源 → 「盘口主动买盘 5 分」恒 0 并标【数据缺失】。
    """
    feat = _intraday_features(min5, profile)
    if feat is None:
        return None
    missing = ["盘口五档主动买盘"]
    notes: list[str] = []
    # 结构判定
    gap = feat["open_gap"] or 0
    dived = feat["above_vwap"] is False and (feat["pullback_depth"] or 0) >= 3
    if gap <= 1.5 and feat["break_early_high"]:
        structure, sname = "A", "低开/平开→突破前高（最优）"
    elif gap > 1.5 and feat["above_vwap"]:
        structure, sname = "B", "高开回落→均价线上方企稳（分歧转强）"
    elif feat["no_new_high_after_surge"] and not dived:
        structure, sname = "C", "冲高后反复无法新高（谨慎）"
    elif dived:
        structure, sname = "D", "冲高跳水跌破均价线（禁止买入）"
    else:
        structure, sname = "C", "结构不典型（谨慎）"
    # 评分（权重同需求第十节；盘口 5 分恒 0）
    s_attack = 20 if structure == "A" else 12 if structure == "B" else 5 if structure == "C" else 0
    s_pullback = 15 if (feat["pullback_depth"] or 0) >= 1 and feat["above_vwap"] else \
        8 if feat["pullback_depth"] else 0
    s_break = 15 if feat["break_early_high"] else 8 if structure == "B" else 0
    vol_ok = (feat["pullback_vol_ratio"] or 0) < 0.8  # 缩量回踩
    s_vol = 15 if vol_ok and feat["break_early_high"] else 10 if vol_ok else 5
    s_vwap = 10 if feat["above_vwap"] else 0
    s_return = 10 if (profile.get("main_net_inflow") or 0) > 0 else 0
    s_board = 10 if (bstats.get("hot_avg") or 0) > 0 else 5 if bstats.get("hot_avg") is not None else 0
    score = s_attack + s_pullback + s_break + s_vol + s_vwap + s_return + 0 + s_board
    notes.append(f"结构{sname}；回踩{feat['pullback_depth']:.1f}%" if feat["pullback_depth"] is not None
                 else f"结构{sname}")
    if vol_ok:
        notes.append("回踩缩量")
    return {
        "score": min(100, score), "structure": structure, "structure_name": sname,
        "notes": notes, "missing": missing,
        "feat": {k: (round(v, 2) if isinstance(v, float) else v)
                 for k, v in feat.items() if k not in ("feat",)},
    }


def _divergence_score(min5: dict[str, Any] | None, profile: dict[str, Any],
                      bstats: dict[str, Any]) -> dict[str, Any] | None:
    """分歧转强 100 分（需求第八节 10 项 × 10 分）。数据缺失返回 None。"""
    if not min5:
        return None
    feat = min5.get("feat") or {}
    flags = {
        "appeared": (feat.get("pullback_depth") or 0) >= 1.0,
        "shrunk_volume": (feat.get("pullback_vol_ratio") or 9) < 0.8,
        "fund_return": (profile.get("main_net_inflow") or 0) > 0,
        "breakout": bool(feat.get("break_early_high")),
        "back_above_vwap": feat.get("above_vwap"),
    }
    got = sum(10 for v in flags.values() if v)
    return {
        "score": got, **flags,
        "notes": [], "missing": [] if feat else ["分时数据"],
    }


# ------------------------------------------------------------------ 第五层：概率模型（估计）

def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _zt_prob(emo_score: float, bstats: dict[str, Any], minute: dict[str, Any] | None,
             fund: dict[str, Any], yaogu: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """涨停概率估计（模型加权，非真实概率；数据缺失则不输出数值）。"""
    if minute is None:
        return {"value": None, "basis": "分时数据【数据缺失】，无法估计", "missing": ["5分钟K线"]}
    lb = profile.get("lianban") or 0
    x = (
        (emo_score / 100) * 0.15
        + (min(100.0, bstats.get("score") or 0) / 100) * 0.20
        + (minute["score"] / 100) * 0.20
        + (fund["score"] / 100) * 0.15
        + (yaogu["score"] / 100) * 0.15
        + min(1.0, (profile.get("volume_ratio") or 0) / 3) * 0.05
        + min(1.0, lb / 4) * 0.05
        + (0.05 if (profile.get("seal_amount") or 0) > 0 else 0)
    )
    v = round(_clamp01(x * 1.15 - 0.05), 2)  # 校准：满配≈0.95，中位≈0.5
    basis = (f"情绪{emo_score:.0f}+板块{bstats.get('score', 0):.0f}+分时{minute['score']}"
             f"+资金{fund['score']:.0f}+妖股{yaogu['score']:.0f} 加权模型估计")
    return {"value": v, "basis": basis, "missing": []}


def _premium_prob(emo_score: float, bstats: dict[str, Any], minute: dict[str, Any] | None,
                  fund: dict[str, Any], divergence: dict[str, Any] | None,
                  profile: dict[str, Any]) -> dict[str, Any]:
    """次日溢价概率估计（模型加权）。"""
    if minute is None:
        return {"value": None, "basis": "分时数据【数据缺失】，无法估计", "missing": ["5分钟K线"]}
    stage_w = {"发酵": 1.0, "分歧": 0.9, "启动": 0.75, "高潮": 0.65, "退潮": 0.2, "观察": 0.55, "未知": 0.3}
    x = (
        (emo_score / 100) * 0.15
        + stage_w.get(bstats.get("stage") or "未知", 0.5) * 0.25
        + (minute["score"] / 100) * 0.15
        + (fund["score"] / 100) * 0.20
        + ((divergence["score"] / 100) if divergence else 0.3) * 0.15
        + (0.10 if (profile.get("main_net_inflow") or 0) > 0 else 0)
    )
    v = round(_clamp01(x * 1.1), 2)
    basis = (f"板块阶段[{bstats.get('stage')}]×0.25 + 资金{fund['score']:.0f} + 分时{minute['score']}"
             f" + 情绪{emo_score:.0f} 加权模型估计")
    return {"value": v, "basis": basis, "missing": [] if divergence else ["分时数据"]}


# ------------------------------------------------------------------ 第六层：准入 / 排除 / 风险收益比

def _gates(bstats: dict[str, Any] | None, yaogu: dict[str, Any],
           fund: dict[str, Any], minute: dict[str, Any] | None,
           ztp: dict[str, Any], pmp: dict[str, Any], composite: float) -> dict[str, Any]:
    stage, _ = _stage_of(bstats)
    board_ok = bstats is not None and (bstats.get("score") or 0) >= 75 and stage != "退潮"
    stock_ok = (yaogu["score"] >= 70 and fund["score"] >= 70
                and minute is not None and minute["score"] >= 75)
    prob_ok = (ztp.get("value") is not None and ztp["value"] >= 0.55
               and pmp.get("value") is not None and pmp["value"] >= 0.60
               and composite >= 80)
    return {"board": board_ok, "stock": stock_ok, "prob": prob_ok}


def _exclusions(profile: dict[str, Any], bstats: dict[str, Any] | None,
                minute: dict[str, Any] | None, emotion: str, rr: float,
                fund: dict[str, Any]) -> list[str]:
    """7 条绝对排除规则（需求第二十节）。返回命中的规则列表（空 = 未排除）。"""
    hits: list[str] = []
    stage, _ = _stage_of(bstats)
    if stage == "退潮":
        hits.append("排除1：板块明显退潮")
    s3 = sum((d.get("main") or 0) for d in (profile.get("flow") or [])[-3:])
    if (profile.get("main_net_inflow") or 0) < 0 and s3 < -3e7:
        hits.append("排除2：主力资金持续大幅流出")
    if minute is not None and minute.get("structure") == "D":
        hits.append("排除3：高开冲顶放巨量跳水破均价线")
    if (bstats is None or (bstats.get("zt_count") or 0) == 0) and (profile.get("change_pct") or 0) > 7:
        hits.append("排除4：单一消息暴涨且板块无资金共振")
    top_lb_chg = bstats.get("top_leader_chg") if bstats else None
    if (profile.get("lianban") or 0) == 0 and top_lb_chg is not None and top_lb_chg < 0:
        hits.append("排除5：纯后排跟风且龙头见顶")
    if rr < 1.5:
        hits.append("排除6：风险收益比不足1.5:1")
    if emotion == "D":
        hits.append("排除7：市场情绪D级禁止追涨")
    return hits


def _risk_reward(profile: dict[str, Any], ztp: dict[str, Any], pmp: dict[str, Any],
                 stop1: float | None) -> dict[str, Any]:
    """期望收益（模型口径，透明列出）：E_profit = zt_prob×10% + premium_prob×3%；
    E_loss = 现价到第一止损位的距离。比率 ≥1.5 才准入。"""
    price = profile.get("price")
    zp = ztp.get("value") or 0
    pp = pmp.get("value") or 0
    e_profit = zp * 10 + pp * 3
    if price and stop1 and price > stop1:
        e_loss = max(1.0, (price - stop1) / price * 100)
    else:
        e_loss = 5.0  # 止损位缺失时的保守假设（如实标注）
    ratio = round(e_profit / e_loss, 2) if e_loss > 0 else 0.0
    return {"e_profit_pct": round(e_profit, 1), "e_loss_pct": round(e_loss, 1), "ratio": ratio,
            "model": "E_profit=zt_prob×10%+premium_prob×3%；E_loss=现价至第一止损距离",
            "assumed": stop1 is None}


# ------------------------------------------------------------------ 交易计划

def _plan(profile: dict[str, Any], min5: dict[str, Any] | None,
          emotion: str) -> dict[str, Any]:
    price = profile.get("price") or 0
    feat = (min5 or {}).get("feat") or {}
    pb_low = feat.get("pullback_low")
    vwap = profile.get("vwap")
    stop1 = round(min(x for x in (pb_low, vwap) if x), 2) if (pb_low or vwap) else None
    stop2 = round(stop1 * 0.96, 2) if stop1 else None
    pos = {"A": "30%", "B": "20%", "C": "10%", "D": "0%"}[emotion]
    return {
        "buy_zone": f"{price * 0.995:.2f}~{price * 1.01:.2f}" if price else "【数据缺失】",
        "endgame_cond": "14:30后：板块仍强 + 站均价线上方 + 缩量回踩后资金回流突破日内高点",
        "stop1": f"{stop1}（分歧低点/分时均价线）" if stop1 else "【数据缺失】",
        "stop2": f"{stop2}（结构止损，约-4%）" if stop2 else "【数据缺失】",
        "tp1": f"{price * 1.08:.2f}~{price * 1.12:.2f}（+8%~12%，不破趋势持有）" if price else "—",
        "tp2": f"{price * 1.18:.2f}（+18%附近，放量滞涨/主力流出分批止盈）" if price else "—",
        "invalidate": "板块进入退潮 / 主力连续2日净流出 / 跌破第二止损位",
        "max_position": pos,
    }


