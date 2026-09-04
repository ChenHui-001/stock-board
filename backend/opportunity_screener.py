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

from . import cache as cache_mod, service
from .providers import registry
from .providers.base import fetch
from .providers.eastmoney import clean_em, search_articles
from .utils import is_trading_now
# 复用价值筛选器已验证的数据源与工具（同仓库内私有函数复用，行为一致）
from .value_screener import (
    _fetch_hot_pool, _fetch_index_quotes, _fetch_zb_pool, _fetch_zt_pool,
    _hot_board_strength, _is_stock_code, _tencent_extra_batch,
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


# ------------------------------------------------------------------ 板块资金流（东财 push2delay，2026-09-04 实测可用）

async def _fetch_board_flow(limit: int = 100) -> dict[str, dict[str, Any]]:
    """东财板块资金流榜（行业口径 m:90 t:2）。

    字段（实测核对，勿凭记忆改）：
      f12=板块代码(BKxxxx) f14=板块名 f3=板块涨跌幅% f62=今日主力净流入(元)
      f164=5日主力净流入(元) f204/f205=领涨股名/代码
    失败返回 {} → 板块评分退回「资金项标【数据缺失】」路径，不阻塞主流程。
    """
    url = ("https://push2delay.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2&fid=f62"
           "&fs=m%%3A90%%20t%%3A2&fields=f12,f14,f3,f62,f164" % limit)
    try:
        resp = await fetch(url, headers={"Referer": "https://quote.eastmoney.com/"})
        data = (resp.json() or {}).get("data") or {}
        out: dict[str, dict[str, Any]] = {}
        for i, r in enumerate(data.get("diff") or []):
            name = str(r.get("f14") or "")
            if not name:
                continue
            out[name] = {
                "name": name,
                "bk_code": r.get("f12") or "",
                "chg": r.get("f3"),           # 板块涨跌幅 %
                "main_today": r.get("f62"),   # 今日主力净流入（元）
                "main_5d": r.get("f164"),     # 5日主力净流入（元）
                "rank": i + 1,                # 按今日主力净流入降序的名次
            }
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("板块资金流获取失败：%s", exc)
        return {}


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
    # 板块来源：涨停池 ∪ 热门榜 ∪ 资金流榜强势板块（前20名且今日主力>0）
    flow_strong = {f["name"] for f in board_flow.values()
                   if (f.get("rank") or 999) <= 20 and (f.get("main_today") or 0) > 0}
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
            "hot_avg": round(hot_avg, 2) if hot_avg is not None else None,
            "relative_strength": round(rel, 2) if rel is not None else None,
            "missing": missing,
        }
    return out


def _stage_of(bstats: dict[str, Any] | None) -> tuple[str, float]:
    """板块阶段与周期分（无数据 → 退潮保守处理：禁止准入）。"""
    if not bstats:
        return "未知", 0.0
    return bstats.get("stage") or "未知", float(bstats.get("stage_score") or 0.0)


# ------------------------------------------------------------------ 第四层-①：妖股基因 100

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
        bars = await registry().kline_min(code, market, 48, klt=5)
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


# ------------------------------------------------------------------ 主流程

def _candidate_base(zt: dict[str, Any], hot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """候选池：涨停池（含炸板回封观察）+ 热门榜，去重。上限 60。"""
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


async def run_screen(force: bool = False) -> dict[str, Any]:
    """机会投资完整流水线（聚合缓存 10 分钟）。"""
    key = "opportunity:screen:v8"
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
    cands = _candidate_base(zt, hot)
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

    # ---- 第三层：硬筛选（需求第六节；5/20日涨幅在深度数据后复核）
    passed: list[dict[str, Any]] = []
    for c in cands:
        chg = c.get("change_pct")
        to = c.get("turnover")
        vr = c.get("volume_ratio")
        amt = c.get("amount")
        main_in = c.get("main_net_inflow")
        if chg is None or not (3 <= chg <= 8):
            continue  # 涨停/涨停上方不属于「分歧转强」买点区间
        if to is None or not (5 <= to <= 20):
            continue
        if vr is None or vr < 2:
            continue
        if amt is None or amt < 2e8:
            continue
        if main_in is None or main_in <= 1e7:
            continue
        passed.append(c)

    # ---- 第四层：深度数据 + 四维评分
    sem = asyncio.Semaphore(6)
    await asyncio.gather(*[_stock_deep(c, sem) for c in passed])
    # 5/20日涨幅复核 + 流通市值过滤（<300亿优先，非绝对淘汰则放宽到 <600 后降妖股分）
    filtered: list[dict[str, Any]] = []
    for c in passed:
        bars = c.get("bars") or []
        c["chg5"] = _chg_n(bars, 5)
        c["chg20"] = _chg_n(bars, 20)
        if c["chg5"] is None or not (-5 <= c["chg5"] <= 20):
            continue
        if c["chg20"] is None or not (-15 <= c["chg20"] <= 30):
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
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market,
        "boards": boards_top,
        "empty": empty,
        "empty_reason": ("【今日无符合条件标的，空仓优于强行交易】"
                         if empty else ""),
        "candidates": final,
        "scan_summary": {
            "candidate_total": len(cands), "hard_passed": len(passed),
            "deep_scored": len(filtered), "final": len(final),
        },
    }
    _cache.put(key, result, _CACHE_TTL)
    return result
