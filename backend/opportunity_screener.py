"""机会投资筛选器 —— A股快速轮动短线策略 V8.0（需求原文：docs/opportunity_strategy_v8.md）。

七层流水线：
  1. 市场情绪四档（A进攻/B结构/C弱势/D退潮）
  2. 板块周期五阶段评分（启动/发酵/高潮/分歧/退潮）
  3. 基础硬筛选（涨幅/换手/量比/成交额/主力净流入/5日/20日涨幅/市值）
  4. 个股四维评分：妖股基因100 / 资金持续性100 / 分歧转强100 / 分时攻击100
  5. zt_prob / premium_prob（加权模型估计，非真实概率）
  6. 三重准入门槛 + 7条绝对排除 + 风险收益比
  7. 最终综合评分排序，最多推荐 2 只；无达标输出【今日无符合条件标的】

数据可得性（2026-09-04 审计，全部 curl/实拉验证）：
  可用：涨停池/炸板池（value_screener 同源）、跌停池 getTopicDTPool（实测），
        实时行情（VWAP/主力净流入/主力净比，东财 f62/f184），30日资金流
        （含超大单/大单），120日日K（涨停基因/5/20日涨幅），5分钟K线（分时结构，
        provider 原生支持 klt=5），板块资金流（push2delay clist f62/f164，
        2026-09-04 补接：今日/5日主力净流入 + 板块涨跌幅）。
  缺失（按需求第二十七节标【数据缺失】，不编造）：盘口五档主动买盘（分时评分
        恒缺 5 分）、消息产业催化（板块分恒缺 10 分）、真实涨停概率
        （zt_prob 仅为模型估计）。

聚合缓存 10 分钟；refresh=1 强制重算。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from . import cache as cache_mod, service, zt_pool
from .providers import registry
from .providers.base import fetch
from .providers.eastmoney import clean_em, search_articles
from .utils import is_trading_now, to_float
# 复用价值筛选器已验证的数据源与工具（同仓库内私有函数复用，行为一致）
from .value_screener import (
    _fetch_board_flow, _fetch_hot_pool, _fetch_index_quotes, _fetch_main_inflow,
    _fetch_zb_pool, _fetch_zt_pool, _hot_board_strength, _is_stock_code,
    _tencent_extra_batch,
)

log = logging.getLogger(__name__)
_cache = cache_mod.cache

_CACHE_TTL = 600.0  # 10 分钟（短线策略日内多刷，但仍避免每 tick 全量重算）

# 涨停判定阈值按板块涨幅制度：主板 10%、创业板/科创板 20%
_ZT_PCT_MAIN = 9.8
_ZT_PCT_20 = 19.8


# ------------------------------------------------------------------ 跌停池

async def _fetch_dt_pool() -> dict[str, Any]:
    """东财跌停池。2026-09-04 curl 实测可用（返回 tc 与 pool 行）。失败返回空结构。"""
    try:
        resp = await fetch(
            "https://push2ex.eastmoney.com/getTopicDTPool",
            params={
                "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                "Pageindex": 0, "pagesize": 400, "sort": "fund:asc",
                "date": datetime.now().strftime("%Y%m%d"),
            },
            headers={"Referer": "https://quote.eastmoney.com/ztb/"},
        )
        data = (resp.json() or {}).get("data") or {}
        pool = data.get("pool") or []
        rows: list[dict[str, Any]] = []
        for r in pool:
            code = str(r.get("c") or "")
            if len(code) != 6:
                continue
            rows.append({
                "code": code,
                "name": r.get("n") or "",
                "change_pct": round((r.get("zdp") or 0) / 100, 2),
                "board": r.get("hybk") or "",
            })
        return {"count": data.get("tc") or len(rows), "rows": rows}
    except Exception as exc:  # noqa: BLE001
        log.warning("跌停池获取失败：%s", exc)
        return {"count": -1, "rows": [], "error": str(exc)}


# ------------------------------------------------------------------ 第一层：市场情绪四档

def _market_emotion(
    zt: dict[str, Any], zb: dict[str, Any], dt: dict[str, Any],
    indices: list[dict[str, Any]], hot: list[dict[str, Any]],
) -> dict[str, Any]:
    """情绪四档 A/B/C/D。各分量带权重打分，缺源分量跳过并归一化。

    分量（权重）：涨停数25 / 连板高度20 / 炸板率20 / 跌停15 / 20cm活跃10 / 温度10。
    D 级硬触发：跌停≥30 或 炸板率≥0.40（宁可错杀，不抄底退潮）。
    """
    missing: list[str] = []
    zt_count = zt.get("count") or 0
    zb_count = zb.get("count") or 0
    rows = zt.get("rows") or []
    max_lb = max((r.get("lianban") or 0) for r in rows) if rows else 0
    active20 = sum(1 for r in rows if str(r.get("code") or "").startswith(("30", "68")))
    total = zt_count + zb_count
    zb_rate = (zb_count / total) if total > 0 else None

    parts: list[tuple[float, float]] = []  # (得分, 权重)
    # 涨停数（25）
    if zt_count >= 100: parts.append((25, 25))
    elif zt_count >= 70: parts.append((20, 25))
    elif zt_count >= 50: parts.append((15, 25))
    elif zt_count >= 30: parts.append((8, 25))
    else: parts.append((3, 25))
    # 连板高度（20）
    if max_lb >= 7: parts.append((20, 20))
    elif max_lb >= 5: parts.append((16, 20))
    elif max_lb >= 3: parts.append((12, 20))
    elif max_lb >= 2: parts.append((6, 20))
    else: parts.append((0, 20))
    # 炸板率（20）：≤10% 满分，>30% 归零
    if zb_rate is not None:
        if zb_rate <= 0.10: parts.append((20, 20))
        elif zb_rate <= 0.20: parts.append((14, 20))
        elif zb_rate <= 0.30: parts.append((7, 20))
        else: parts.append((0, 20))
    else:
        missing.append("炸板率")
    # 跌停数（15）
    dt_count = dt.get("count", -1)
    if isinstance(dt_count, int) and dt_count >= 0:
        if dt_count == 0: parts.append((15, 15))
        elif dt_count <= 5: parts.append((12, 15))
        elif dt_count <= 15: parts.append((6, 15))
        elif dt_count <= 30: parts.append((2, 15))
        else: parts.append((0, 15))
    else:
        missing.append("跌停池")
    # 20cm 活跃（10）
    if active20 >= 5: parts.append((10, 10))
    elif active20 >= 2: parts.append((6, 10))
    else: parts.append((2, 10))
    # 市场温度（10）：热门榜平均涨幅
    chgs = [h.get("change_pct") for h in hot if h.get("change_pct") is not None]
    avg_chg = sum(chgs) / len(chgs) if chgs else None
    if avg_chg is not None:
        if avg_chg >= 3: parts.append((10, 10))
        elif avg_chg >= 1.5: parts.append((6, 10))
        elif avg_chg >= 0: parts.append((3, 10))
        else: parts.append((0, 10))
    else:
        missing.append("市场温度")

    weight = sum(w for _s, w in parts) or 1.0
    score = round(sum(s for s, _w in parts) / weight * 100, 1)

    idx_chg = next((i.get("change_pct") for i in indices if i.get("code") == "000001"), None)
    if (isinstance(dt_count, int) and dt_count >= 30) or (zb_rate is not None and zb_rate >= 0.40):
        emotion = "D"
    elif score >= 70 and (idx_chg is None or idx_chg > -1):
        emotion = "A"
    elif score >= 50:
        emotion = "B"
    elif score >= 30:
        emotion = "C"
    else:
        emotion = "D"

    tier = {
        "A": ("强势进攻环境", "进攻", "70%~100%"),
        "B": ("结构性机会环境", "谨慎", "40%~70%"),
        "C": ("弱势轮动环境", "防守", "0%~30%"),
        "D": ("风险/退潮环境", "空仓", "0%"),
    }[emotion]
    return {
        "emotion": emotion, "emotion_name": tier[0],
        "position": tier[1], "position_range": tier[2],
        "emotion_score": score,
        "zt_count": zt_count, "zb_count": zb_count,
        "dt_count": dt_count if isinstance(dt_count, int) and dt_count >= 0 else None,
        "zb_rate": round(zb_rate, 3) if zb_rate is not None else None,
        "max_lianban": max_lb, "active_20cm": active20,
        "avg_hot_chg": round(avg_chg, 2) if avg_chg is not None else None,
        "index_chg": idx_chg,
        "missing": missing,
    }



# ------------------------------------------------------------------ 板块消息/产业催化（东财全文检索）

_catalyst_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_CATALYST_TTL = 1800.0  # 新闻催化 30 分钟缓存，足够新鲜且不刷接口


async def _fetch_catalysts(
    names: list[str], sem: asyncio.Semaphore | None = None,
) -> dict[str, dict[str, Any] | None]:
    """板块消息/产业催化判定：东财全文检索板块名，统计近 24h 命中文章。

    返回 {板块名: 结果}：
      - {"count": N>0, "titles": [≤2条标题], "latest_time": "HH:MM"} → 有催化
      - {"count": 0, ...} → 查询成功但确无近期资讯（真实结论，不算缺失）
      - None → 检索失败（标【数据缺失】，不编造）
    """
    out: dict[str, dict[str, Any] | None] = {}
    todo: list[str] = []
    now_ts = time.time()
    cutoff = now_ts - _CATALYST_TTL
    for n in names:
        hit = _catalyst_cache.get(n)
        if hit and hit[0] > cutoff:
            out[n] = hit[1]
        else:
            todo.append(n)
    if not todo:
        return out
    sem = sem or asyncio.Semaphore(4)
    cutoff_dt = datetime.now() - timedelta(hours=24)

    async def _one(name: str) -> None:
        try:
            async with sem:
                rows = await search_articles(name, page_size=10)
        except Exception as exc:  # noqa: BLE001
            log.debug("板块催化检索 %s 失败: %s", name, exc)
            _catalyst_cache[name] = (now_ts, None)
            out[name] = None
            return
        recent: list[tuple[str, str]] = []
        for r in rows:
            d = str(r.get("date") or "")
            try:
                dt = datetime.strptime(d[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if dt >= cutoff_dt:
                recent.append((d[11:16], clean_em(r.get("title") or "")))
        res = {
            "count": len(recent),
            "titles": [t for _hm, t in recent[:2]],
            "latest_time": recent[0][0] if recent else None,
        }
        _catalyst_cache[name] = (now_ts, res)
        out[name] = res

    await asyncio.gather(*[_one(n) for n in todo])
    return out


# ------------------------------------------------------------------ 第二层：板块周期五阶段

async def _board_stats(
    zt_rows: list[dict[str, Any]], zb_rows: list[dict[str, Any]],
    hot_rows: list[dict[str, Any]], index_chg: float | None,
    board_flow: dict[str, dict[str, Any]] | None = None,
    catalysts: dict[str, dict[str, Any] | None] | None = None,
) -> dict[str, dict[str, Any]]:
    """按板块聚合涨停/炸板/热门/资金流/消息催化数据，输出板块周期阶段与 0-100 评分。

    板块评分模型（需求第四节，100 分制全量落地）：
      涨停梯队15+龙头强度15+相对大盘10+扩散5+分歧转强5+成交代理10
      +今日资金20+连续资金10（push2delay 实测 f62/f164）
      +消息产业催化10（东财全文检索板块名，近24h命中：≥3条=10分/1-2条=6分/0条=0分）
    「查过但 0 命中」是真实结论不算缺失；仅检索失败标【数据缺失】。
    catalysts 显式传入时直接使用（测试/复用），None 时内部并发拉取（带30分钟缓存）。
    资金流榜中主力净流入强（前 20 名且 >0）但暂无涨停的板块也纳入展示。
    """
    board_flow = board_flow or {}
    zt_agg: dict[str, list[dict[str, Any]]] = {}
    for r in zt_rows:
        b = r.get("board") or ""
        if b:
            zt_agg.setdefault(b, []).append(r)
    zb_agg: dict[str, int] = {}
    for r in zb_rows:
        b = r.get("hybk") or ""
        if b:
            zb_agg[b] = zb_agg.get(b, 0) + 1
    hot_agg: dict[str, list[float]] = {}
    for r in hot_rows:
        b = r.get("board") or ""
        c = r.get("change_pct")
        if b and c is not None:
            hot_agg.setdefault(b, []).append(c)

    out: dict[str, dict[str, Any]] = {}
    # 板块来源：涨停池 ∪ 热门榜 ∪ 资金流榜强势板块
    # 强势 = 前20名且今日主力>0；盘前/停牌等 f62 不可得时保底展示前12名
    #（资金项届时标【数据缺失】，不编造），避免盘前板块列表空白。
    def _flow_strong() -> set[str]:
        strong: set[str] = set()
        for f in board_flow.values():
            rk = f.get("rank")
            if not rk:
                continue
            mt = f.get("main_today")
            if (rk <= 20 and (mt or 0) > 0) or (rk <= 12 and mt is None):
                strong.add(f["name"])
        return strong

    flow_strong = _flow_strong()
    board_names = set(zt_agg) | set(hot_agg) | flow_strong
    cats = catalysts if catalysts is not None else await _fetch_catalysts(list(board_names))
    for b in board_names:
        fl = board_flow.get(b) or {}
        ztrs = zt_agg.get(b, [])
        zt_count = len(ztrs)
        lb_list = sorted((r.get("lianban") or 1) for r in ztrs)
        max_lb = lb_list[-1] if lb_list else 0
        zb_count = zb_agg.get(b, 0)
        hot_chgs = hot_agg.get(b, [])
        hot_avg = sum(hot_chgs) / len(hot_chgs) if hot_chgs else None
        rel = (hot_avg - index_chg) if (hot_avg is not None and index_chg is not None) else None

        # --- 阶段判定（当日快照的估计，非完整历史推演；如实标注口径）
        flow_chg = fl.get("chg")
        if zt_count == 0:
            stage, stage_score = "观察", 55  # 无涨停：启动前夜或冷门
            if (hot_avg is not None and hot_avg < -1) or (flow_chg is not None and flow_chg < -1):
                stage, stage_score = "退潮", 30
        elif zt_count >= 6 and zb_count >= max(3, zt_count // 2):
            stage, stage_score = "分歧", 80 if rel is not None and rel > 1 else 65
        elif zt_count >= 8 and max_lb >= 4 and zb_count <= 2:
            stage, stage_score = "高潮", 78  # 一致性强 → 次日分歧风险，压分
        elif zt_count >= 4 and max_lb >= 2:
            stage, stage_score = "发酵", 85
        elif zt_count >= 2:
            stage, stage_score = "启动", 65
        else:
            stage, stage_score = "启动", 60
        if stage == "退潮":
            stage_score = 30

        # --- 评分（可用 60 分制 → 归一化 100）
        got = 0.0
        # 涨停梯队 15：家数 + 连板结构
        got += min(10, zt_count * 1.4) + (5 if max_lb >= 3 else 2 if max_lb >= 2 else 0)
        # 龙头强度 15：最高连板 + 封单额
        seal = max((r.get("seal_amount") or 0) for r in ztrs) if ztrs else 0
        got += min(8, max_lb * 2.5) + (7 if seal >= 1e8 else 4 if seal > 0 else 0 if not ztrs else 2)
        # 相对大盘 10
        if rel is not None:
            got += max(0, min(10, 5 + rel * 1.5))
        # 扩散 5：多只涨停分散在梯队中（非单一妖股独撑）
        if zt_count >= 4 and len([x for x in lb_list if x >= 2]) >= 2:
            got += 5
        elif zt_count >= 2:
            got += 3
        # 分歧转强 5：炸板少 = 承接强（快照代理）
        if zt_count > 0:
            got += 5 if zb_count == 0 else 3 if zb_count <= 2 else 0
        # 成交代理 10：热门榜均涨幅（涨幅≠资金，代理口径标注）
        if hot_avg is not None:
            got += max(0, min(10, hot_avg * 2))
        # 板块资金流入 20（东财 f62 今日主力净流入，元）
        missing: list[str] = []
        mt, m5d = fl.get("main_today"), fl.get("main_5d")
        if mt is not None:
            got += (20 if mt >= 1e9 else 16 if mt >= 5e8
                    else 12 if mt >= 2e8 else 8 if mt > 0 else 0)
        else:
            missing.append("板块资金流入")
        # 连续资金流入 10（f164 五日主力；五日与今日双正 = 持续性最强）
        if m5d is not None:
            got += 10 if (m5d > 0 and (mt or 0) > 0) else 5 if m5d > 0 else 0
        else:
            missing.append("连续资金流入(板块级)")
        # 消息/产业催化 10（东财全文检索板块名，近24h命中；0命中=真实结论不标缺失）
        cat = cats.get(b)
        if cat is None:
            missing.append("消息/产业催化")
        elif cat["count"] >= 3:
            got += 10
        elif cat["count"] >= 1:
            got += 6
        score = round(min(100.0, got), 1)

        # 强势股：板块内涨停股（连板降序取前3）+ 资金流领涨股
        zt_stocks = [{"code": r.get("code"), "name": r.get("name") or "",
                      "lianban": r.get("lianban") or 1,
                      "change_pct": r.get("change_pct")}
                     for r in sorted(ztrs, key=lambda x: (x.get("lianban") or 1),
                                     reverse=True)[:3]]
        leader = ({"name": fl["leader_name"], "code": fl["leader_code"],
                   "chg": fl.get("leader_chg")}
                  if fl.get("leader_name") else None)

        out[b] = {
            "name": b, "score": score, "stage": stage, "stage_score": stage_score,
            "is_ferment": stage == "发酵",
            "zt_count": zt_count, "zb_count": zb_count, "max_lianban": max_lb,
            "has_leader": max_lb >= 2,
            "fund_today": round(mt / 1e8, 2) if mt is not None else None,   # 今日主力净流入（亿）
            "fund_5d": round(m5d / 1e8, 2) if m5d is not None else None,    # 5日主力净流入（亿）
            "fund_rank": fl.get("rank"),                                    # 资金流榜名次
            "board_chg": flow_chg,                                          # 板块涨跌幅 %
            "catalyst": cat,   # {"count","titles","latest_time"} 或 None=检索失败
            "zt_stocks": zt_stocks,   # 板块内涨停股（连板降序前3）
            "leader": leader,         # 资金流领涨股 {name,code,chg} 或 None
            "hot_avg": round(hot_avg, 2) if hot_avg is not None else None,
            "relative_strength": round(rel, 2) if rel is not None else None,
            "missing": missing,
        }
    return out



# ------------------------------------------------------------------ 4~6 层评分引擎
# 已拆分至 opportunity_scores.py（体检 M7）；re-export 保持兼容。
from .opportunity_scores import (  # noqa: F401  re-export
    _ZT_PCT_MAIN,
    _ZT_PCT_20,
    _count_zt,
    _yaogu_score,
    _fund_score,
    _fetch_min5,
    _intraday_features,
    _minute_score,
    _divergence_score,
    _clamp01,
    _zt_prob,
    _premium_prob,
    _gates,
    _exclusions,
    _risk_reward,
    _plan,
)
# ------------------------------------------------------------------ 主流程

def _candidate_base(zt: dict[str, Any], hot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """候选池：**昨日**涨停池（今日分歧转强买点）+ 热门榜，去重。上限 60。

    为什么用昨日池：策略买的是「昨日涨停、今日 3~8% 分歧转强」，硬筛的
    涨幅 3~8% 区间天然排除今日已涨停股（+10%/+20cm）——候选若来自今日
    涨停池，会在数学上 100% 被硬筛清零（2026-09-09 实弹：60 候选 58 只
    死于「涨幅不在3~8%」，hard_passed=0）。lianban/zttj 取昨日口径，
    正是妖股评分需要的「昨日连板高度」。
    """
    out: dict[str, dict[str, Any]] = {}
    for r in (zt.get("rows") or []):
        code = r.get("code") or ""
        if not _is_stock_code(code, r.get("market") or "SH"):
            continue
        out[f"{code}.{r['market']}"] = {
            "code": code, "market": r["market"], "name": r.get("name") or "",
            "board": r.get("board") or "", "lianban": r.get("lianban") or 0,
            "change_pct": r.get("change_pct"), "turnover": r.get("turnover"),
            "volume_ratio": r.get("volume_ratio"), "seal_amount": r.get("seal_amount"),
        }
    for h in hot:
        key = f"{h['code']}.{h['market']}"
        if key not in out and _is_stock_code(h["code"], h["market"]):
            out[key] = {**h, "seal_amount": None}
    rows = list(out.values())
    return rows[:60]


async def _stock_deep(profile: dict[str, Any], sem: asyncio.Semaphore) -> None:
    """单股深度数据：30日资金流 + 120日日K + 今日5分钟K，三路并发，尽力而为。"""
    code, market = profile["code"], profile["market"]

    async def _flow() -> list[dict[str, Any]]:
        try:
            days = await service.flow_cached(code, market, 30)
            return [{"date": d.date, "main": d.main, "xl": d.xl, "lg": d.lg,
                     "main_pct": d.main_pct} for d in days]
        except Exception as exc:  # noqa: BLE001
            log.debug("flow %s 失败: %s", code, exc)
            return []

    async def _kline() -> list[dict[str, Any]]:
        try:
            bars = await service.kline_cached(code, market, 120)
            return [{"date": b.date, "open": b.open, "close": b.close, "high": b.high,
                     "low": b.low, "amount": b.amount, "change_pct": b.change_pct}
                    for b in bars]
        except Exception as exc:  # noqa: BLE001
            log.debug("kline %s 失败: %s", code, exc)
            return []

    async def _min5() -> list[dict[str, Any]]:
        async with sem:
            return await _fetch_min5(code, market)

    flow, bars, m5 = await asyncio.gather(_flow(), _kline(), _min5())
    profile["flow"] = flow
    profile["bars"] = bars
    profile["min5"] = m5


def _chg_n(bars: list[dict[str, Any]], n: int) -> float | None:
    """近 N 日涨幅（今收 vs N 日前收）。"""
    if len(bars) < n + 1:
        return None
    base = bars[-n - 1].get("close")
    last = bars[-1].get("close")
    if not base or not last:
        return None
    return round((last / base - 1) * 100, 2)


def _prev_trade_dates(today: datetime | None = None, back: int = 5) -> list[str]:
    """自昨日往前的候选交易日列表（YYYYMMDD，含周末自动跳过的余地）。

    不引入完整交易日历：东财池接口对非交易日返回空，逐日回退即可命中最
    近一个真实交易日（节假日最多连休 8 天，back=5 覆盖绝大多数场景）。
    """
    base = today or datetime.now()
    return [(base - timedelta(days=i)).strftime("%Y%m%d") for i in range(1, back + 1)]


async def _fetch_prev_zt_pool() -> dict[str, Any]:
    """昨日（最近一个已收盘交易日）涨停池，供「昨日涨停今日分歧」候选。"""
    for d in _prev_trade_dates():
        pool = await zt_pool.get_zt_pool(trade_date=d)
        if (pool.get("count") or 0) > 0:
            return pool
    return {"count": 0, "rows": []}


def _pick_watch(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """观察名单：未全过三重准入但综合分≥80，≤2 只。

    策略「宁缺毋滥」的透明化——让用户看到今日最接近达标者与差距，
    明确不构成买入建议；与 final 互斥（not _all_pass）。
    注意不再要求无排除项：情绪 D（排除7）本就否决买入，若连观察都清空，
    弱市页面只剩一句空话、用户无从知道「差在哪」（2026-09-09 用户反馈）。
    排除原因仍完整展示在 action_reason 里。
    """
    return [r for r in results if not r["_all_pass"] and r["composite"] >= 80][:2]


async def run_screen(force: bool = False) -> dict[str, Any]:
    """机会投资完整流水线（聚合缓存 10 分钟）。"""
    key = "opportunity:screen:v9"
    if not force:
        cached = _cache.peek(key)
        if cached:
            return cached

    # ---- 第一层：市场环境（并发）
    indices, zt, zb, dt, hot, board_flow = await asyncio.gather(
        _fetch_index_quotes(), _fetch_zt_pool(), _fetch_zb_pool(),
        _fetch_dt_pool(), _fetch_hot_pool(40), _fetch_board_flow(),
    )
    index_chg = next((i.get("change_pct") for i in indices if i.get("code") == "000001"), None)
    market = _market_emotion(zt, zb, dt, indices, hot)

    # ---- 第二层：板块周期
    bstats_map = await _board_stats(zt.get("rows") or [], zb.get("rows") or [],
                                    hot, index_chg, board_flow)
    hot_rank = {b["name"]: i for i, b in enumerate(
        sorted(bstats_map.values(), key=lambda x: x["score"], reverse=True))}
    for b in bstats_map.values():
        b["hot_rank"] = hot_rank.get(b["name"])
    boards_top = sorted(bstats_map.values(), key=lambda x: x["score"], reverse=True)[:12]
    boards_top = [{k: v for k, v in b.items() if k != "top_leader_chg"} for b in boards_top]

    # ---- 候选池 + 批量行情
    # 候选用「昨日涨停池」（分歧转强），市场情绪/板块周期仍用今日池（当日市况）
    zt_prev = await _fetch_prev_zt_pool()
    cands = _candidate_base(zt_prev, hot)
    if cands:
        keys = [(c["code"], c["market"]) for c in cands]
        try:
            quotes, _src = await registry().quotes(keys)
        except Exception as exc:  # noqa: BLE001
            log.warning("候选批量行情失败：%s", exc)
            quotes = {}
        extra_map = await _tencent_extra_batch(cands)
        for c in cands:
            q = quotes.get(f"{c['code']}.{c['market']}")
            if q:
                c.update({
                    "price": q.price, "prev_close": q.prev_close, "open": q.open,
                    "change_pct": q.change_pct, "turnover": q.turnover,
                    "volume_ratio": q.volume_ratio, "amount": q.amount,
                    "board": c.get("board") or q.board,
                    "vwap": q.vwap, "deviation_pct": q.deviation_pct,
                    "main_net_inflow": q.main_net_inflow, "main_net_pct": q.main_net_pct,
                })
            c.update(extra_map.get(f"{c['code']}.{c['market']}") or {})
        # push2delay 实时资金兜底：registry 频控切腾讯源时补回主净/净比/换手/量比
        main_map = await _fetch_main_inflow(cands)
        for c in cands:
            for k, v in (main_map.get(c["code"]) or {}).items():
                if v is not None:
                    c[k] = v

    # ---- 第三层：硬筛选（需求第六节；5/20日涨幅在深度数据后复核）
    passed: list[dict[str, Any]] = []
    hard_dropped: dict[str, int] = {}  # 淘汰原因 → 只数（透明化：让用户看到卡在哪关）
    for c in cands:
        chg = c.get("change_pct")
        to = c.get("turnover")
        vr = c.get("volume_ratio")
        amt = c.get("amount")
        main_in = c.get("main_net_inflow")
        reasons: list[str] = []
        if chg is None or not (3 <= chg <= 8):
            reasons.append("涨幅不在3~8%")  # 涨停/涨停上方不属于「分歧转强」买点区间
        if to is None or not (5 <= to <= 20):
            reasons.append("换手不在5~20%")
        if vr is None or vr < 2:
            reasons.append("量比<2")
        if amt is None or amt < 2e8:
            reasons.append("成交额<2亿")
        if main_in is None or main_in <= 1e7:
            reasons.append("主力净流入≤1000万")
        for r in reasons:
            hard_dropped[r] = hard_dropped.get(r, 0) + 1
        if not reasons:
            passed.append(c)

    # ---- 第四层：深度数据 + 四维评分
    sem = asyncio.Semaphore(6)
    await asyncio.gather(*[_stock_deep(c, sem) for c in passed])
    # 5/20日涨幅复核 + 流通市值过滤（<300亿优先，非绝对淘汰则放宽到 <600 后降妖股分）
    # chg5 上限 35 / chg20 上限 60：2~3 连板妖股的近端涨幅天然 >20%（20cm 双板
    # 甚至 >40%），旧阈值(20/30)会把「分歧转强」的目标群体在数学上清零，
    # 与策略妖股基因直接矛盾；上限仍是风险护栏（排除已翻倍的高位股）
    filtered: list[dict[str, Any]] = []
    for c in passed:
        bars = c.get("bars") or []
        c["chg5"] = _chg_n(bars, 5)
        c["chg20"] = _chg_n(bars, 20)
        if c["chg5"] is None or not (-5 <= c["chg5"] <= 35):
            continue
        if c["chg20"] is None or not (-15 <= c["chg20"] <= 60):
            continue
        fm = c.get("float_mv")
        if fm is not None and fm > 600:
            continue
        filtered.append(c)

    # 板块内候选计数（资金共振代理）
    board_cand: dict[str, int] = {}
    for c in filtered:
        b = c.get("board") or ""
        if (c.get("main_net_inflow") or 0) > 0:
            board_cand[b] = board_cand.get(b, 0) + 1

    scored: list[dict[str, Any]] = []
    for c in filtered:
        bstats = bstats_map.get(c.get("board") or "")
        if bstats:
            top = max((r.get("lianban") or 0) for r in (zt.get("rows") or [])
                      if r.get("board") == c.get("board")) if any(
                r.get("board") == c.get("board") for r in (zt.get("rows") or [])) else 0
            top_chg = max((r.get("change_pct") or 0) for r in (zt.get("rows") or [])
                          if r.get("board") == c.get("board") and (r.get("lianban") or 0) == top) if top else None
            bstats = {**bstats, "top_leader_chg": top_chg}
        yaogu = _yaogu_score(c, bstats or {}, c.get("bars") or [])
        fund = _fund_score(c, c.get("bars") or [], board_cand.get(c.get("board") or "", 0))
        m5raw = await _fetch_min5(c["code"], c["market"]) if not c.get("min5") else c.get("min5")
        minute = _minute_score(c, m5raw, bstats or {})
        divergence = _divergence_score(minute, c, bstats or {})
        ztp = _zt_prob(market["emotion_score"], bstats or {}, minute, fund, yaogu, c)
        pmp = _premium_prob(market["emotion_score"], bstats or {}, minute, fund, divergence, c)
        # 最终综合评分（需求第十四节）
        stage_score = (bstats or {}).get("stage_score", 0.0)
        composite = (
            stage_score / 100 * 20
            + fund["score"] / 100 * 20
            + yaogu["score"] / 100 * 20
            + ((divergence["score"] / 100 * 15) if divergence else 0)
            + ((minute["score"] / 100 * 10) if minute else 0)
            + market["emotion_score"] / 100 * 5
            + (ztp["value"] or 0) * 5
            + (pmp["value"] or 0) * 5
        )
        composite = round(composite, 1)
        gates = _gates(bstats, yaogu, fund, minute, ztp, pmp, composite)
        scored.append({
            "c": c, "bstats": bstats, "yaogu": yaogu, "fund": fund, "minute": minute,
            "divergence": divergence, "ztp": ztp, "pmp": pmp,
            "composite": composite, "gates": gates,
        })

    # ---- 交易计划 / 风险收益比 / 排除规则 / 三重准入
    results: list[dict[str, Any]] = []
    for s in scored:
        c, bstats = s["c"], s["bstats"]
        plan = _plan(c, s["minute"], market["emotion"])
        stop1_txt = plan["stop1"].split("（")[0]
        try:
            stop1_val = float(stop1_txt)
        except (ValueError, TypeError):
            stop1_val = None
        rr = _risk_reward(c, s["ztp"], s["pmp"], stop1_val)
        excl = _exclusions(c, bstats, s["minute"], market["emotion"], rr["ratio"], s["fund"])
        gates = s["gates"]
        all_pass = all(gates.values()) and not excl
        stars = ("★★★★★" if s["composite"] >= 90 else "★★★★" if s["composite"] >= 85
                 else "★★★" if s["composite"] >= 80 else "★★" if s["composite"] >= 75 else "★")
        action = "买入" if all_pass and s["composite"] >= 85 else \
            "观察" if all_pass or (s["composite"] >= 80 and not excl) else "放弃"
        stage = (bstats or {}).get("stage", "未知")
        if all_pass and s["composite"] >= 85:
            reason = (f"板块处于{stage}期且评分{(bstats or {}).get('score', 0):.0f}，"
                      f"资金持续性{s['fund']['score']:.0f}分、分时{s['minute']['structure']}级结构，"
                      f"分歧转强后主力仍在净流入——买的是明日资金延续，而不是今日涨幅。")
        elif action == "观察":
            reason = "接近但未全过三重准入或存在排除项，列入观察，不构成买入建议。"
        else:
            reason = "；".join(excl) if excl else "三重准入未全部通过。"
        missing = sorted({*(bstats or {}).get("missing", []), *s["yaogu"]["missing"],
                           *s["fund"]["missing"], *(s["minute"] or {}).get("missing", []),
                           *s["ztp"].get("missing", []), *s["pmp"].get("missing", []),
                           "盘口主动买盘"})
        results.append({
            "code": c["code"], "market": c["market"], "name": c.get("name") or "",
            "board": c.get("board") or "",
            "price": c.get("price"), "change_pct": c.get("change_pct"),
            "turnover": c.get("turnover"), "volume_ratio": c.get("volume_ratio"),
            "amount": round((c.get("amount") or 0) / 1e8, 2),   # 亿元
            "float_mv": c.get("float_mv"), "total_mv": c.get("total_mv"),
            "main_net_inflow": c.get("main_net_inflow"),
            "main_net_pct": c.get("main_net_pct"),
            "chg5": c.get("chg5"), "chg20": c.get("chg20"),
            "vwap_dev": c.get("deviation_pct"),
            "lianban": c.get("lianban") or 0,
            "board_info": {k: v for k, v in (bstats or {}).items()
                           if k not in ("top_leader_chg", "hot_rank")},
            "yaogu": s["yaogu"], "fund": s["fund"], "minute": s["minute"],
            "divergence": s["divergence"], "zt_prob": s["ztp"], "premium_prob": s["pmp"],
            "composite": s["composite"], "stars": stars, "gates": gates,
            "risk_reward": rr, "plan": plan, "action": action, "action_reason": reason,
            "missing": missing,
            "_sort": (s["composite"], stage_score, s["fund"]["score"],
                      (s["divergence"] or {}).get("score", 0),
                      s["yaogu"]["score"], (s["minute"] or {}).get("score", 0),
                      s["pmp"].get("value") or 0, s["ztp"].get("value") or 0),
            "_all_pass": all_pass,
        })

    # ---- 排序（需求第二十二节）后取三重准入全过者，最多 2 只
    results.sort(key=lambda r: r["_sort"], reverse=True)
    final = [r for r in results if r["_all_pass"]][:2]
    for i, r in enumerate(final, 1):
        r["rank"] = i
        r.pop("_sort", None)
        r.pop("_all_pass", None)
    empty = not final
    watch = _pick_watch(results)
    for i, r in enumerate(watch, 1):
        r["rank"] = i
        r.pop("_sort", None)
        r.pop("_all_pass", None)
    scan = {
        "candidate_total": len(cands), "hard_passed": len(passed),
        "deep_scored": len(filtered), "final": len(final),
        "hard_dropped": dict(sorted(hard_dropped.items(),
                                    key=lambda kv: kv[1], reverse=True)),
    }
    empty_reason = "【今日无符合条件标的，空仓优于强行交易】" if empty else ""
    if empty:
        if hard_dropped:
            top2 = "、".join(f"{k}({v}只)" for k, v in
                             list(hard_dropped.items())[:2])
            empty_reason += f" 硬筛淘汰主因：{top2}。"
        if watch:
            empty_reason += (f" 最接近达标者已列入观察（{len(watch)} 只，见下方），"
                             "仅盯盘参考，不构成买入建议。")
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market,
        "boards": boards_top,
        "empty": empty,
        "empty_reason": empty_reason,
        "candidates": final,
        "watchlist": watch,
        "scan_summary": scan,
    }
    _cache.put(key, result, _CACHE_TTL)
    return result
