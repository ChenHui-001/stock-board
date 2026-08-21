"""A 股快速轮动量化选股引擎（价值投资菜单）。

按「基本面合格 + 行业景气 + 板块强势 + 资金流入 + 个股强于板块 + 买点合理 +
风险可控」的思路选股，重点适配 A 股板块快速轮动、情绪驱动、游资机构共博弈。

流程：市场环境判断 → 板块强度 → 候选池（涨停池 + 热门榜）→ 暴雷硬过滤 →
多维评分（基本面/板块/资金/量价筹码/情绪妖股）→ 风险扣分 → 分级池与买卖信号。

数据原则（与项目一致）：只用真实数据源（腾讯/东财/同花顺/AkShare），
任何取不到的指标标记【数据缺失】并计入数据完整度，绝不编造。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from . import cache as cache_mod
from .config import settings

_cache = cache_mod.cache
from .providers import ProviderError, registry
from .providers.base import fetch

log = logging.getLogger("value_screener")

_CACHE_TTL = 900.0  # 15 分钟聚合缓存（避免反复打接口）
_CANDIDATE_LIMIT = 40  # 候选池上限（涨停池 + 热门榜去重）
_SCORE_CACHE_TTL = 3600.0

# 指数代码（上证/深成/创业板/科创50/沪深300）
_INDEX_KEYS = [
    ("000001", "SH", "上证指数"),
    ("399001", "SZ", "深证成指"),
    ("399006", "SZ", "创业板指"),
    ("000688", "SH", "科创50"),
    ("000300", "SH", "沪深300"),
]

# 市场状态分类（按涨跌家数/涨停数/指数强度粗判）
def _market_state(indices: list[dict[str, Any]], zt_count: int, avg_chg: float) -> dict[str, Any]:
    """市场状态分类 A-F 与进攻等级 0-100。"""
    sh = next((i for i in indices if i["code"] == "000001"), None)
    sh_chg = (sh or {}).get("change_pct") or 0.0
    cyb = next((i for i in indices if i["code"] == "399006"), None)
    cyb_chg = (cyb or {}).get("change_pct") or 0.0

    # 涨停数作为情绪温度
    if zt_count >= 60:
        emotion = "亢奋"
    elif zt_count >= 35:
        emotion = "活跃"
    elif zt_count >= 15:
        emotion = "平稳"
    else:
        emotion = "低迷"

    if sh_chg >= 1.5 and cyb_chg >= 1.5 and zt_count >= 50:
        state, name = "A", "强趋势市场"
        attack = 85
    elif sh_chg >= 0.5 and zt_count >= 35:
        state, name = "B", "结构性牛市"
        attack = 70
    elif abs(sh_chg) < 1.0 and zt_count >= 30:
        state, name = "C", "快速轮动市场"
        attack = 60
    elif abs(sh_chg) < 1.0:
        state, name = "D", "震荡存量市场"
        attack = 45
    elif sh_chg <= -1.0 and zt_count < 25:
        state, name = "E", "情绪退潮市场"
        attack = 25
    else:
        state, name = "F", "系统性风险市场"
        attack = 10

    if sh_chg <= -2.5:
        attack = min(attack, 15)
    return {
        "state": state,
        "name": name,
        "attack": attack,
        "emotion": emotion,
        "sh_change_pct": round(sh_chg, 2),
        "cyb_change_pct": round(cyb_chg, 2),
    }


async def _fetch_index_quotes() -> list[dict[str, Any]]:
    try:
        quotes, _src = await registry().quotes([(c, m) for c, m, _n in _INDEX_KEYS])
    except Exception as exc:  # noqa: BLE001
        log.warning("指数行情获取失败：%s", exc)
        return []
    out: list[dict[str, Any]] = []
    for code, _m, name in _INDEX_KEYS:
        q = quotes.get(f"{code}.{_m}")
        if not q:
            continue
        out.append({
            "code": code, "name": q.name or name,
            "price": q.price, "change_pct": q.change_pct,
            "amount": q.amount,
        })
    return out


async def _fetch_zt_pool() -> dict[str, Any]:
    """东财涨停池（含连板/板块/换手）。失败返回空结构。"""
    try:
        resp = await fetch(
            "https://push2ex.eastmoney.com/getTopicZTPool",
            params={
                "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                "Pageindex": 0, "pagesize": 400, "sort": "fbt:asc",
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
            market = "SH" if code.startswith(("6", "9", "5")) else "SZ"
            rows.append({
                "code": code, "market": market,
                "name": r.get("n") or "",
                "change_pct": round((r.get("zdp") or 0) / 100, 2),
                "turnover": r.get("hs"),
                "volume_ratio": r.get("lb"),
                "lianban": r.get("lbc"),           # 连板数
                "seal_amount": r.get("fund"),        # 封单额
                "board": r.get("hybk") or "",        # 所属板块
            })
        return {"count": data.get("tc") or len(rows), "rows": rows}
    except Exception as exc:  # noqa: BLE001
        log.warning("涨停池获取失败：%s", exc)
        return {"count": 0, "rows": []}


async def _fetch_hot_pool(limit: int = 20) -> list[dict[str, Any]]:
    """热门榜（涨幅榜 + 成交额榜）作为候选补充。"""
    out: dict[str, dict[str, Any]] = {}
    try:
        hot = await registry().hot(limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("热门榜获取失败：%s", exc)
        return []
    for bucket in ("gainers", "actives"):
        for q in hot.get(bucket) or []:
            key = f"{q.code}.{q.market}"
            if key not in out:
                out[key] = {
                    "code": q.code, "market": q.market, "name": q.name,
                    "change_pct": q.change_pct, "turnover": q.turnover,
                    "volume_ratio": q.volume_ratio, "board": q.board,
                    "amount": q.amount, "lianban": 0,
                }
    return list(out.values())


def _is_stock_code(code: str, market: str) -> bool:
    if market not in ("SH", "SZ"):
        return False
    if code.startswith(("4", "8", "92")):
        return False  # 北交所暂不纳入
    if code.startswith(("5", "1", "15", "16", "18")):  # ETF/LOF/可转债
        return False
    if market == "SH" and not code.startswith(("60", "68", "00")):
        return False
    if market == "SZ" and not code.startswith(("00", "30")):
        return False
    return True


def _merge_candidates(zt: dict[str, Any], hot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """候选池：涨停池优先（带连板/板块），热门榜补充；去重 + 普通股过滤。"""
    merged: dict[str, dict[str, Any]] = {}
    for r in zt.get("rows") or []:
        if not _is_stock_code(r["code"], r["market"]):
            continue
        merged[f"{r['code']}.{r['market']}"] = r
    for r in hot:
        if not _is_stock_code(r["code"], r["market"]):
            continue
        key = f"{r['code']}.{r['market']}"
        if key not in merged:
            merged[key] = r
    return list(merged.values())[:_CANDIDATE_LIMIT]


# ------------------------------------------------------------------ 单股数据

async def _tencent_extra(code: str, market: str) -> dict[str, Any]:
    """腾讯行情补充字段：PE / PB / 总市值 / 量比 / 换手 / 振幅（qt.gtimg.cn）。"""
    prefix = "sh" if market == "SH" else "sz"
    try:
        resp = await fetch(
            f"https://qt.gtimg.cn/q={prefix}{code}",
            headers={"Referer": "https://gu.qq.com/"},
        )
        line = resp.text.strip().split("~")
        if len(line) < 50:
            return {}
        def _f(i: int):
            try:
                v = float(line[i])
                return v if v == v else None  # NaN -> None
            except (ValueError, IndexError):
                return None
        return {
            "pe": _f(39),
            "pb": _f(49),
            "total_mv": _f(45),       # 总市值（亿）
            "turnover": _f(38),
            "volume_ratio": _f(43),
            "amplitude": _f(43),      # 振幅
            "amount": _f(37) * 10000 if _f(37) else None,  # 成交额（万 -> 元）
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("腾讯补充行情 %s 失败：%s", code, exc)
        return {}


async def _stock_profile(cand: dict[str, Any]) -> dict[str, Any]:
    """单只候选的完整数据：行情 + 补充字段 + 财务 + 资金流 + K线。全部尽力而为。"""
    code, market = cand["code"], cand["market"]
    profile: dict[str, Any] = {
        "code": code, "market": market, "name": cand.get("name") or "",
        "board": cand.get("board") or "", "lianban": cand.get("lianban") or 0,
        "zt_change_pct": cand.get("change_pct"),
    }
    # 行情
    try:
        quotes, _ = await registry().quotes([(code, market)])
        q = quotes.get(f"{code}.{market}")
        if q:
            profile.update({
                "price": q.price, "change_pct": q.change_pct,
                "turnover": q.turnover, "volume_ratio": q.volume_ratio,
                "amount": q.amount, "board": profile["board"] or q.board,
            })
    except Exception:  # noqa: BLE001
        pass
    extra = await _tencent_extra(code, market)
    profile.update(extra)
    # 财务（最近 8 期）
    try:
        fin, _src = await registry().financials(code, market, 8)
        profile["financials"] = [
            {
                "period": f.period, "revenue_yoy": f.revenue_yoy,
                "net_profit_yoy": f.net_profit_yoy,
                "net_profit": f.net_profit, "roe": f.roe,
                "gross_margin": f.gross_margin, "debt_ratio": f.debt_ratio,
            }
            for f in fin
        ]
    except Exception:  # noqa: BLE001
        profile["financials"] = []
    # 资金流（近 30 日）
    try:
        flow, _src = await registry().fund_flow(code, market, 30)
        profile["flow"] = [{"date": d.date, "main": d.main, "main_pct": d.main_pct} for d in flow]
    except Exception:  # noqa: BLE001
        profile["flow"] = []
    # K线（近 30 日，算相对强度）
    try:
        bars, _src = await registry().kline(code, market, 30)
        profile["kline"] = [
            {"date": b.date, "close": b.close, "change_pct": b.change_pct, "volume": b.volume}
            for b in bars
        ]
    except Exception:  # noqa: BLE001
        profile["kline"] = []
    return profile


# ------------------------------------------------------------------ 评分引擎

def _financial_score(fin: list[dict[str, Any]]) -> dict[str, Any]:
    """基本面评分（满分 50）：成长 13 + 质量 12 + 价值 8 + 行业 10 的可用子集。"""
    if not fin:
        return {"score": 0, "detail": "财务数据缺失", "completeness": 0}
    latest = fin[0]
    prev = fin[1] if len(fin) > 1 else {}
    # 成长（营收/净利同比，最近 4 期趋势）
    growth_pts = 0
    rev_yoys = [f.get("revenue_yoy") for f in fin[:4] if f.get("revenue_yoy") is not None]
    np_yoys = [f.get("net_profit_yoy") for f in fin[:4] if f.get("net_profit_yoy") is not None]
    if np_yoys:
        latest_np = np_yoys[0]
        if latest_np > 30: growth_pts += 6
        elif latest_np > 10: growth_pts += 4
        elif latest_np > 0: growth_pts += 2
        elif latest_np > -10: growth_pts -= 1
        else: growth_pts -= 3
        if len(np_yoys) >= 2 and np_yoys[0] > np_yoys[1]:
            growth_pts += 3  # 加速
    if rev_yoys:
        latest_rev = rev_yoys[0]
        if latest_rev > 20: growth_pts += 4
        elif latest_rev > 5: growth_pts += 2
        elif latest_rev > 0: growth_pts += 1
        else: growth_pts -= 2
    growth_pts = max(0, min(13, growth_pts))

    # 质量（ROE / 毛利率 / 负债率）
    quality_pts = 0
    roe = latest.get("roe")
    if roe is not None:
        if roe > 15: quality_pts += 5
        elif roe > 8: quality_pts += 3
        elif roe > 0: quality_pts += 1
        else: quality_pts -= 2
    gm = latest.get("gross_margin")
    if gm is not None:
        if gm > 40: quality_pts += 4
        elif gm > 20: quality_pts += 2
        elif gm > 0: quality_pts += 1
    dr = latest.get("debt_ratio")
    if dr is not None:
        if dr < 40: quality_pts += 3
        elif dr < 60: quality_pts += 1
        elif dr > 75: quality_pts -= 2
    quality_pts = max(0, min(12, quality_pts))

    # 价值（仅在有 PE 时加分，估值过低/亏损扣分由风险层处理）
    value_pts = 0
    np_trend = [f.get("net_profit_yoy") for f in fin[:3] if f.get("net_profit_yoy") is not None]
    if np_trend and all(x > 0 for x in np_trend):
        value_pts += 5  # 连续正增长 = 基本面扎实
    if len(np_trend) >= 2 and np_trend[0] > np_trend[1] > 0:
        value_pts += 3
    value_pts = max(0, min(8, value_pts))

    # 子项满分 13+12+8=33，换算到基本面 0-50 全带（与策略口径一致）
    total = (growth_pts + quality_pts + value_pts) * 50 / 33
    detail = f"成长{growth_pts}/13 质量{quality_pts}/12 价值{value_pts}/8"
    return {"score": round(min(50, total), 1), "detail": detail, "completeness": 1}


def _board_score(profile: dict[str, Any], board_strength: dict[str, float]) -> dict[str, Any]:
    """板块评分（满分 10）：候选所属板块的市场强度。"""
    b = profile.get("board") or ""
    if not b:
        return {"score": 0, "detail": "板块未知", "completeness": 0}
    strength = board_strength.get(b)
    if strength is None:
        return {"score": 3, "detail": f"板块「{b}」无强度数据", "completeness": 0}
    pts = max(0, min(10, round(strength * 10)))
    return {"score": pts, "detail": f"板块强度 {strength:.2f}", "completeness": 1}


def _flow_score(profile: dict[str, Any]) -> dict[str, Any]:
    """资金评分（满分 12）：近 5 日主力资金拐点。

    重点找「30 日流出 → 20 日流出 → 10 日企稳 → 5/3/1 日流入」的资金拐点，
    禁止只看 30 日累计。
    """
    flow = profile.get("flow") or []
    if len(flow) < 2:
        return {"score": 0, "detail": "资金数据缺失", "completeness": 0}
    last5 = flow[-5:]
    main_1 = sum(d["main"] for d in last5[-1:])
    main_3 = sum(d["main"] for d in last5[-3:])
    main_5 = sum(d["main"] for d in last5)
    main_30 = sum(d["main"] for d in flow)

    pts = 0
    if main_1 > 0: pts += 3
    elif main_1 < 0: pts -= 2
    if main_3 > 0: pts += 3
    elif main_3 < 0: pts -= 2
    if main_5 > 0: pts += 2
    if main_30 > 0: pts += 1
    # 资金拐点：30 日流出但 5 日转流入 → 额外加分
    if main_30 < 0 and main_5 > 0:
        pts += 3
    pts = max(0, min(12, pts))
    detail = f"1日{main_1/1e8:.1f}亿 3日{main_3/1e8:.1f}亿 5日{main_5/1e8:.1f}亿 30日{main_30/1e8:.1f}亿"
    return {"score": pts, "detail": detail, "completeness": 1}


def _volume_score(profile: dict[str, Any]) -> dict[str, Any]:
    """量价筹码评分（满分 8）：量比/换手/涨跌幅/相对强度。"""
    pts = 0
    change = profile.get("change_pct")
    if change is not None:
        if change > 5: pts += 3
        elif change > 2: pts += 2
        elif change > 0: pts += 1
        elif change < -3: pts -= 2
    vr = profile.get("volume_ratio")
    if vr is not None:
        if vr > 1.5: pts += 2   # 放量
        elif vr > 1.0: pts += 1
        elif vr < 0.6: pts -= 1  # 缩量
    to = profile.get("turnover")
    if to is not None:
        if 2 <= to <= 15: pts += 1  # 活跃但不疯狂
        elif to > 25: pts -= 1      # 换手过猛
    # 涨停池内的候选本身带强势属性
    if profile.get("lianban"):
        pts += 2
    pts = max(0, min(8, pts))
    return {"score": pts, "detail": f"涨跌{change}% 量比{vr} 换手{to}%", "completeness": 1}


def _emotion_score(profile: dict[str, Any]) -> dict[str, Any]:
    """情绪妖股评分（满分 12）：连板高度 + 换手活跃度 + 涨停属性。"""
    pts = 0
    lb = profile.get("lianban") or 0
    if lb >= 5: pts += 6
    elif lb >= 3: pts += 5
    elif lb >= 2: pts += 4
    elif lb >= 1: pts += 3
    to = profile.get("turnover")
    if to is not None:
        if 8 <= to <= 20: pts += 4   # 高换手
        elif 3 <= to < 8: pts += 2
        elif to > 25: pts += 1       # 过热但仍是焦点
    chg = profile.get("change_pct")
    if chg is not None and chg > 5: pts += 2
    pts = max(0, min(12, pts))
    return {"score": pts, "detail": f"连板{lb} 换手{to}%", "completeness": 1}


def _risk_score(profile: dict[str, Any], fin_score: dict[str, Any]) -> dict[str, Any]:
    """风险评分（0-100，>60 禁入核心池）与风险扣分。"""
    fin = profile.get("financials") or []
    risk = 0
    notes: list[str] = []
    name = profile.get("name") or ""
    if "ST" in name.upper() or "*ST" in name.upper():
        risk += 40
        notes.append("ST/*ST")
    if not fin:
        risk += 10
        notes.append("财务数据缺失")
    else:
        latest = fin[0]
        np_yoy = latest.get("net_profit_yoy")
        if np_yoy is not None and np_yoy < -30:
            risk += 20
            notes.append(f"净利同比 {np_yoy:.0f}%")
        dr = latest.get("debt_ratio")
        if dr is not None and dr > 80:
            risk += 15
            notes.append(f"负债率 {dr:.0f}%")
        if latest.get("net_profit") is not None and latest["net_profit"] < 0:
            risk += 15
            notes.append("亏损")
    # 估值风险：PE 过高
    pe = profile.get("pe")
    if pe is not None and pe > 120:
        risk += 10
        notes.append(f"PE {pe:.0f}")
    # 涨幅过大追高风险
    chg = profile.get("change_pct")
    if chg is not None and chg > 9.5:
        risk += 10
        notes.append("接近涨停追高")
    # 连板过高接力风险
    if (profile.get("lianban") or 0) >= 6:
        risk += 15
        notes.append(f"{profile['lianban']} 连板高位")
    risk = max(0, min(100, risk))
    return {"score": risk, "notes": notes, "completeness": 1}


def _buy_score(profile: dict[str, Any], scores: dict[str, Any]) -> dict[str, Any]:
    """买点评分（0-100）：价格位置 + 量价 + 资金 + 板块 + 情绪的综合入场时机。"""
    pts = 0.0
    # 量价健康
    vr = profile.get("volume_ratio")
    chg = profile.get("change_pct")
    if vr is not None and vr >= 1.5 and chg is not None and chg > 0:
        pts += 20  # 放量上涨
    elif chg is not None and chg > 5:
        pts += 12
    elif chg is not None and chg < -3:
        pts -= 15  # 下跌不买
    # 资金
    flow = profile.get("flow") or []
    if len(flow) >= 3:
        main_3 = sum(d["main"] for d in flow[-3:])
        if main_3 > 0: pts += 20
        elif main_3 < 0: pts -= 10
    # 板块
    board_pts = scores["board"]["score"] if scores.get("board") else 0
    pts += board_pts * 4  # 满分 10 -> 40
    # 情绪（连板梯队）
    lb = profile.get("lianban") or 0
    if 1 <= lb <= 3: pts += 12   # 启动/发酵期最优
    elif 4 <= lb <= 5: pts += 6
    elif lb >= 6: pts -= 8       # 高位接力风险
    # 风险否决
    risk = scores.get("risk", {}).get("score", 0)
    if risk > 60: pts -= 40
    pts = max(0, min(100, round(pts)))
    return {"score": pts, "detail": f"量价+资金+板块+情绪综合"}


def _signal(profile: dict[str, Any], total: float, buy: int, risk: int) -> str:
    """买卖信号：BREAKOUT_BUY / PULLBACK_BUY / BUY / WATCH / REDUCE / AVOID。"""
    chg = profile.get("change_pct") or 0
    vr = profile.get("volume_ratio") or 0
    lb = profile.get("lianban") or 0
    if risk > 60:
        return "AVOID"
    if total >= 75 and buy >= 70:
        if chg > 5 and vr >= 1.5 and lb >= 1:
            return "BREAKOUT_BUY"
        if 0 < chg <= 5 and vr < 1.2 and lb >= 1:
            return "PULLBACK_BUY"
        return "BUY"
    if total >= 60:
        return "WATCH"
    if chg < -4:
        return "REDUCE"
    return "AVOID"


def _grade(total: float) -> tuple[str, str]:
    if total >= 85: return "S", "核心机会池"
    if total >= 78: return "A", "重点观察池"
    if total >= 70: return "B", "待确认池"
    if total >= 60: return "C", "观察池"
    return "D", "淘汰"


def _completeness(profile: dict[str, Any]) -> int:
    """数据完整度 0-100：行情/财务/资金/K线各占权重。"""
    pts = 20
    if profile.get("price") is not None: pts += 20
    if profile.get("financials"): pts += 25
    if profile.get("flow"): pts += 20
    if profile.get("kline"): pts += 15
    return min(100, pts)


async def _analyze_one(
    cand: dict[str, Any], board_strength: dict[str, float]
) -> dict[str, Any] | None:
    profile = await _stock_profile(cand)
    if profile.get("price") is None and not profile.get("financials"):
        return None  # 核心数据全缺，跳过
    fin_score = _financial_score(profile.get("financials") or [])
    board = _board_score(profile, board_strength)
    flow = _flow_score(profile)
    volume = _volume_score(profile)
    emotion = _emotion_score(profile)
    risk = _risk_score(profile, fin_score)
    scores = {"finance": fin_score, "board": board, "flow": flow,
              "volume": volume, "emotion": emotion, "risk": risk}
    # 综合评分：基本面 + 板块 + 资金 + 量价 + 情绪 - 风险扣分
    total = (
        fin_score["score"] + board["score"] + flow["score"]
        + volume["score"] + emotion["score"]
        - (risk["score"] / 10 if risk["score"] > 20 else 0)
    )
    total = max(0.0, min(100.0, round(total, 1)))
    buy = _buy_score(profile, scores)
    trade = round(total * 0.7 + buy["score"] * 0.3, 1)
    grade, grade_name = _grade(total)
    completeness = _completeness(profile)
    return {
        "code": profile["code"], "market": profile["market"],
        "name": profile["name"], "board": profile["board"],
        "price": profile.get("price"), "change_pct": profile.get("change_pct"),
        "pe": profile.get("pe"), "pb": profile.get("pb"),
        "total_mv": profile.get("total_mv"), "turnover": profile.get("turnover"),
        "volume_ratio": profile.get("volume_ratio"), "lianban": profile.get("lianban"),
        "financials_count": len(profile.get("financials") or []),
        "scores": {k: v["score"] for k, v in scores.items()},
        "score_details": {k: v.get("detail", "") for k, v in scores.items()},
        "risk_notes": risk.get("notes", []),
        "total_score": total, "buy_score": buy["score"], "trade_score": trade,
        "grade": grade, "grade_name": grade_name,
        "signal": _signal(profile, total, buy["score"], risk["score"]),
        "completeness": completeness,
    }


# ------------------------------------------------------------------ 板块强度

async def _fetch_board_strength(zt_rows: list[dict[str, Any]]) -> dict[str, float]:
    """板块强度：按涨停池中候选所属板块聚合（涨停数 + 平均涨幅）。

    东财板块涨幅榜接口不稳定（频控），用涨停池聚合作为主数据源；
    涨停数越多、平均涨幅越高 → 板块强度越强（0-1）。
    """
    boards: dict[str, list[float]] = {}
    for r in zt_rows:
        b = r.get("board") or ""
        if not b:
            continue
        boards.setdefault(b, []).append(r.get("change_pct") or 0.0)
    strength: dict[str, float] = {}
    for b, chgs in boards.items():
        # 涨停数权重 60% + 平均涨幅 40%
        count = len(chgs)
        avg = sum(chgs) / len(chgs) if chgs else 0
        s = min(1.0, count / 10) * 0.6 + min(1.0, max(0, avg) / 10) * 0.4
        strength[b] = round(s, 3)
    return strength


# ------------------------------------------------------------------ 对外入口

async def run_screen(force: bool = False) -> dict[str, Any]:
    """完整选股流程（聚合缓存 15 分钟）。"""
    key = "value:screen"
    if not force:
        cached = _cache.peek(key)
        if cached:
            return cached

    # 第一层：市场环境
    indices = await _fetch_index_quotes()
    zt = await _fetch_zt_pool()
    hot = await _fetch_hot_pool(20)
    avg_chg = 0.0
    if hot:
        chgs = [h.get("change_pct") for h in hot if h.get("change_pct") is not None]
        avg_chg = sum(chgs) / len(chgs) if chgs else 0.0
    market = _market_state(indices, zt.get("count") or 0, avg_chg)

    # 板块强度
    board_strength = await _fetch_board_strength(zt.get("rows") or [])

    # 候选池
    candidates = _merge_candidates(zt, hot)
    log.info("价值选股：市场=%s(%s) 涨停=%s 候选=%s",
             market["name"], market["state"], zt.get("count"), len(candidates))

    # 逐股评分（并发，限流友好）
    sem = asyncio.Semaphore(8)
    async def _limited(c: dict[str, Any]):
        async with sem:
            return await _analyze_one(c, board_strength)
    results = await asyncio.gather(*[_limited(c) for c in candidates])
    stocks = [r for r in results if r is not None]
    stocks.sort(key=lambda s: s["trade_score"], reverse=True)

    # 分级池（按策略语义分池，而非按总分一刀切）：
    # core=基本面合格+风险低（基本面负责能不能买）；trend=综合分尚可（板块/资金/量价）；
    # emotion=连板梯队（情绪负责还能不能继续炒）。
    def _fin(s): return (s.get("scores") or {}).get("finance") or 0
    def _risk(s): return (s.get("scores") or {}).get("risk") or 0
    pool_core = sorted(
        [s for s in stocks if _fin(s) >= 25 and _risk(s) <= 40],
        key=lambda s: s["trade_score"], reverse=True)[:10]
    pool_trend = sorted(
        [s for s in stocks if s["total_score"] >= 55 and s["grade"] in ("A", "B", "C")],
        key=lambda s: s["trade_score"], reverse=True)[:10]
    pool_emotion = sorted(
        [s for s in stocks if (s.get("lianban") or 0) >= 2],
        key=lambda s: (s["lianban"], s["trade_score"]), reverse=True)[:10]

    # 最强板块 TOP10（按强度）
    board_top = sorted(board_strength.items(), key=lambda kv: kv[1], reverse=True)[:10]

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": {
            **market,
            "indices": indices,
            "zt_count": zt.get("count") or 0,
            "candidate_count": len(candidates),
        },
        "board_top": [{"name": b, "strength": s} for b, s in board_top],
        "pools": {
            "core": pool_core,
            "trend": pool_trend,
            "emotion": pool_emotion,
        },
        "stocks": stocks,
        "session": __import__("backend.service", fromlist=["session_info"]).session_info(),
    }
    _cache.put(key, result, _CACHE_TTL)
    return result
