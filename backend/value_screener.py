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

from . import cache as cache_mod, valuecfg
from .config import settings

_cache = cache_mod.cache
from .providers import ProviderError, registry
from .providers.base import fetch

log = logging.getLogger("value_screener")

_CACHE_TTL = 900.0  # 15 分钟聚合缓存（避免反复打接口）
_CANDIDATE_LIMIT = 40  # 候选池上限（涨停池 + 热门榜 + 价值基准 去重）
# 价值投资基准池:沪深 300 蓝筹 + 部分行业龙头。
# 这些是公认的价值投资候选,在涨停池为空(弱势市)/ 全部高估时仍能
# 提供样本,让"价值投资"页面真的选得到价值股,而非纯涨停池的情绪博弈。
# 列表按"市值 + 流动性 + 行业代表性"挑选,均为真实可交易的 A 股。
_VALUE_BASELINE: list[dict[str, Any]] = [
    {"code": "600519", "market": "SH", "name": "贵州茅台"},
    {"code": "601318", "market": "SH", "name": "中国平安"},
    {"code": "600036", "market": "SH", "name": "招商银行"},
    {"code": "601398", "market": "SH", "name": "工商银行"},
    {"code": "600028", "market": "SH", "name": "中国石化"},
    {"code": "601857", "market": "SH", "name": "中国石油"},
    {"code": "600000", "market": "SH", "name": "浦发银行"},
    {"code": "600030", "market": "SH", "name": "中信证券"},
    {"code": "600276", "market": "SH", "name": "恒瑞医药"},
    {"code": "600887", "market": "SH", "name": "伊利股份"},
    {"code": "601166", "market": "SH", "name": "兴业银行"},
    {"code": "601288", "market": "SH", "name": "农业银行"},
    {"code": "601988", "market": "SH", "name": "中国银行"},
    {"code": "600050", "market": "SH", "name": "中国联通"},
    {"code": "601012", "market": "SH", "name": "隆基绿能"},
    {"code": "600585", "market": "SH", "name": "海螺水泥"},
    {"code": "600900", "market": "SH", "name": "长江电力"},
    {"code": "601088", "market": "SH", "name": "中国神华"},
    {"code": "000858", "market": "SZ", "name": "五粮液"},
    {"code": "000333", "market": "SZ", "name": "美的集团"},
    {"code": "000651", "market": "SZ", "name": "格力电器"},
    {"code": "000001", "market": "SZ", "name": "平安银行"},
    {"code": "000002", "market": "SZ", "name": "万科A"},
    {"code": "000063", "market": "SZ", "name": "中兴通讯"},
    {"code": "000725", "market": "SZ", "name": "京东方A"},
    {"code": "000538", "market": "SZ", "name": "云南白药"},
    {"code": "000568", "market": "SZ", "name": "泸州老窖"},
    {"code": "000876", "market": "SZ", "name": "新希望"},
    {"code": "002415", "market": "SZ", "name": "海康威视"},
    {"code": "002594", "market": "SZ", "name": "比亚迪"},
    {"code": "300750", "market": "SZ", "name": "宁德时代"},
    {"code": "002475", "market": "SZ", "name": "立讯精密"},
    {"code": "300059", "market": "SZ", "name": "东方财富"},
    {"code": "600196", "market": "SH", "name": "复星医药"},
    {"code": "601888", "market": "SH", "name": "中国中免"},
    {"code": "601628", "market": "SH", "name": "中国人寿"},
    {"code": "600104", "market": "SH", "name": "上汽集团"},
    {"code": "601800", "market": "SH", "name": "中国交建"},
    {"code": "601668", "market": "SH", "name": "中国建筑"},
    {"code": "600048", "market": "SH", "name": "保利发展"},
]
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
def _market_state(
    indices: list[dict[str, Any]], zt_count: int, avg_chg: float,
    zb_count: int = 0,
) -> dict[str, Any]:
    """市场状态分类 A-F 与进攻等级 0-100。

    zb_count 为炸板数（涨停后开板）。炸板率是情绪退潮的领先指标：
    ≥50% 说明封板资金扛不住抛压，次日常补跌，需压低进攻等级。
    """
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

    # 炸板率：封板资金承接力度的直接体现
    total_limit = zt_count + zb_count
    zb_rate = round(zb_count / total_limit * 100, 1) if total_limit > 0 else 0.0
    if zb_rate >= 50:
        attack = int(attack * 0.6)
        emotion = "退潮"
    elif zb_rate >= 35:
        attack = int(attack * 0.75)
        if emotion == "亢奋":
            emotion = "活跃"
    return {
        "state": state,
        "name": name,
        "attack": attack,
        "emotion": emotion,
        "sh_change_pct": round(sh_chg, 2),
        "cyb_change_pct": round(cyb_chg, 2),
        "zb_count": zb_count,
        "zb_rate": zb_rate,
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


async def _fetch_zb_pool() -> dict[str, Any]:
    """东财炸板池（涨停后开板）数量，用于计算炸板率。

    炸板率 = 炸板数 / (涨停数 + 炸板数)，是情绪退潮的领先指标：
    封板资金扛不住抛压 → 次日常伴随高位股补跌。失败返回空结构。
    """
    try:
        resp = await fetch(
            "https://push2ex.eastmoney.com/getTopicZBPool",
            params={
                "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                "Pageindex": 0, "pagesize": 400, "sort": "fbt:asc",
                "date": datetime.now().strftime("%Y%m%d"),
            },
            headers={"Referer": "https://quote.eastmoney.com/ztb/"},
        )
        data = (resp.json() or {}).get("data") or {}
        pool = data.get("pool") or []
        return {"count": data.get("tc") or len(pool), "rows": pool}
    except Exception as exc:  # noqa: BLE001
        log.debug("炸板池获取失败：%s", exc)
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


def _merge_candidates(
    zt: dict[str, Any], hot: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """候选池:涨停池(情绪/题材)+ 热门榜(资金) + 价值基准池(蓝筹),去重 + 普通股过滤。

    优先级:
    1) 涨停池 - 带连板/板块,情绪博弈的核心样本;
    2) 热门榜 - 资金关注度,捕捉主流资金动向;
    3) 价值基准池 - 沪深 300 蓝筹,在弱势市/全部高估时仍能选到价值股。
    合并去重后截断到 _CANDIDATE_LIMIT。
    """
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
    # 价值基准池:仅在前面两个池未覆盖时才加入(避免重复抓数据)
    for r in _VALUE_BASELINE:
        if not _is_stock_code(r["code"], r["market"]):
            continue
        key = f"{r['code']}.{r['market']}"
        if key not in merged:
            # 补齐 hot 需要的字段,后续 _stock_profile 会用真实数据覆盖
            merged[key] = {**r, "change_pct": 0.0, "turnover": None,
                            "volume_ratio": None, "lianban": 0, "board": ""}
    return list(merged.values())[:_CANDIDATE_LIMIT]


# ------------------------------------------------------------------ 单股数据

# 腾讯 qt.gtimg.cn 原始字段索引（实测核对，勿凭记忆改）：
#   f[30]=行情时间(YYYYMMDDHHMMSS) f[37]=成交额(万) f[38]=换手率%
#   f[39]=PE(TTM)                  f[43]=振幅%      f[45]=总市值(亿)
#   f[46]=PB                       f[49]=量比
# 历史坑：曾把 pb 取到 f[49](量比)、volume_ratio 取到 f[43](振幅)，
# 导致估值分与量价分系统性失真（PE/PB 档位、量比信号全部错值）。
_T_PE, _T_PB, _T_MV, _T_TO, _T_VR, _T_AMP = 39, 46, 45, 38, 49, 43


def _parse_tencent_extra(fields: list[str]) -> dict[str, Any]:
    """从腾讯行情原始字段数组解析补充指标。"""
    if len(fields) < 50:
        return {}

    def _f(i: int) -> float | None:
        try:
            v = float(fields[i])
            return v if v == v else None  # NaN -> None
        except (ValueError, IndexError):
            return None

    raw_time = fields[30] if len(fields) > 30 else ""
    quote_time = ""
    if len(raw_time) >= 14:
        quote_time = f"{raw_time[8:10]}:{raw_time[10:12]}:{raw_time[12:14]}"
    return {
        "pe": _f(_T_PE),
        "pb": _f(_T_PB),          # ← 修正：PB 在 46（原错取 49=量比）
        "total_mv": _f(_T_MV),    # 总市值（亿）
        "turnover": _f(_T_TO),
        "volume_ratio": _f(_T_VR),  # ← 修正：量比在 49（原错取 43=振幅）
        "amplitude": _f(_T_AMP),    # 振幅%
        "amount": _f(37) * 10000 if _f(37) else None,  # 成交额（万 -> 元）
        "quote_time": quote_time,
    }


async def _tencent_extra_batch(
    cands: list[dict[str, Any]], batch_size: int = 50
) -> dict[str, dict[str, Any]]:
    """批量拉取腾讯补充字段，返回 {code.market: 指标dict}。

    qt.gtimg.cn 支持一次请求多只（逗号分隔）。候选 40 只时由 40 次请求
    降到 1 次，选股耗时与触发频控的概率都显著下降。
    """
    out: dict[str, dict[str, Any]] = {}
    if not cands:
        return out
    for i in range(0, len(cands), batch_size):
        batch = cands[i:i + batch_size]
        symbols = ",".join(
            ("sh" if c["market"] == "SH" else "sz") + c["code"] for c in batch
        )
        try:
            resp = await fetch(
                f"https://qt.gtimg.cn/q={symbols}",
                headers={"Referer": "https://gu.qq.com/"},
            )
            text = resp.text or ""
        except Exception as exc:  # noqa: BLE001
            log.debug("腾讯批量补充行情失败：%s", exc)
            continue
        for line in text.split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            body = line.split("=", 1)[1].strip().strip('"')
            f = body.split("~")
            if len(f) < 50:
                continue
            code = str(f[2] or "")
            if len(code) != 6:
                continue
            market = "SH" if code.startswith(("6", "9", "5")) else "SZ"
            out[f"{code}.{market}"] = _parse_tencent_extra(f)
    return out


async def _tencent_extra(code: str, market: str) -> dict[str, Any]:
    """单只腾讯补充字段（保留兼容入口，内部走批量实现）。"""
    got = await _tencent_extra_batch([{"code": code, "market": market}])
    return got.get(f"{code}.{market}") or {}


async def _stock_profile(
    cand: dict[str, Any], extra: "dict[str, Any] | None" = None
) -> dict[str, Any]:
    """单只候选的完整数据：行情 + 补充字段 + 财务 + 资金流 + K线。全部尽力而为。

    extra 为 run_screen 预先批量取好的腾讯补充字段（PE/PB/市值/量比等）；
    传入时不再逐只请求腾讯接口。财务/资金/K线三路并发取，缩短单股耗时。
    """
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
    # 腾讯补充字段：优先用预取的批量结果，缺失时才单独补一次
    if extra is None:
        extra = await _tencent_extra(code, market)
    profile.update(extra or {})

    async def _fin() -> list[dict[str, Any]]:
        try:
            fin, _src = await registry().financials(code, market, 8)
            return [
                {
                    "period": f.period, "revenue_yoy": f.revenue_yoy,
                    "net_profit_yoy": f.net_profit_yoy,
                    "net_profit": f.net_profit, "roe": f.roe,
                    "gross_margin": f.gross_margin, "debt_ratio": f.debt_ratio,
                    "ocf_to_netprofit": getattr(f, "ocf_to_netprofit", None),
                }
                for f in fin
            ]
        except Exception:  # noqa: BLE001
            return []

    async def _flow() -> list[dict[str, Any]]:
        try:
            flow, _src = await registry().fund_flow(code, market, 30)
            return [{"date": d.date, "main": d.main, "main_pct": d.main_pct} for d in flow]
        except Exception:  # noqa: BLE001
            return []

    async def _kline() -> list[dict[str, Any]]:
        try:
            # 60 日 K 线:既给 20 日价格位置评分用,又给位置自适应(60 日窗口)用。
            bars, _src = await registry().kline(code, market, 60)
            return [
                {"date": b.date, "close": b.close, "change_pct": b.change_pct,
                 "volume": b.volume, "high": b.high, "low": b.low}
                for b in bars
            ]
        except Exception:  # noqa: BLE001
            return []

    # 三路并发：财务 / 资金 / K线互不依赖
    fin_list, flow_list, kline_list = await asyncio.gather(_fin(), _flow(), _kline())
    profile["financials"] = fin_list
    profile["flow"] = flow_list
    profile["kline"] = kline_list
    return profile


# ------------------------------------------------------------------ 评分引擎

def _financial_score(
    profile_or_fin: "dict[str, Any] | list[dict[str, Any]]",
    fin: "list[dict[str, Any]] | None" = None,
    board_strength: "dict[str, float] | None" = None,
) -> dict[str, Any]:
    """基本面评分（满分 50）：成长 12 + 质量 10 + 估值 18 + 现金流 4 + 行业 6。

    新签名支持估值(PE+PB+PEG)与行业景气评估。旧调用方式
    `_financial_score(fin_list)` 兼容：把 list 当作 fin、profile 视作空。
    """
    # 向后兼容:第一个位置参数可以是 list（旧 API）或 dict（新 API）
    if isinstance(profile_or_fin, list):
        fin = profile_or_fin
        profile: dict[str, Any] = {}
    else:
        profile = profile_or_fin
        if fin is None:
            fin = profile.get("financials") or []
    board_strength = board_strength or {}

    if not fin:
        return {
            "score": 0,
            "detail": "财务数据缺失",
            "completeness": 0,
            "value_metrics": {"pe_band": None, "pb_band": None,
                              "peg_band": None, "ocf_band": None,
                              "industry_band": None},
        }
    latest = fin[0]
    has_valuation = profile.get("pe") is not None or profile.get("pb") is not None

    # -------- 成长 12:净利润/营收同比 + 加速趋势
    growth_pts = 0
    np_yoys = [f.get("net_profit_yoy") for f in fin[:4] if f.get("net_profit_yoy") is not None]
    rev_yoys = [f.get("revenue_yoy") for f in fin[:4] if f.get("revenue_yoy") is not None]
    if np_yoys:
        latest_np = np_yoys[0]
        if latest_np > 30:
            growth_pts += 6
        elif latest_np > 10:
            growth_pts += 4
        elif latest_np > 0:
            growth_pts += 2
        elif latest_np > -10:
            growth_pts -= 1
        else:
            growth_pts -= 3
        if len(np_yoys) >= 2 and np_yoys[0] > np_yoys[1]:
            growth_pts += 3  # 加速
    if rev_yoys:
        latest_rev = rev_yoys[0]
        if latest_rev > 20:
            growth_pts += 3
        elif latest_rev > 5:
            growth_pts += 2
        elif latest_rev > 0:
            growth_pts += 1
        else:
            growth_pts -= 2
    growth_pts = max(0, min(12, growth_pts))

    # -------- 质量 10:ROE / 毛利率 / 负债率
    quality_pts = 0
    roe = latest.get("roe")
    if roe is not None:
        if roe > 15:
            quality_pts += 4
        elif roe > 8:
            quality_pts += 2
        elif roe > 0:
            quality_pts += 1
        else:
            quality_pts -= 1
    gm = latest.get("gross_margin")
    if gm is not None:
        if gm > 40:
            quality_pts += 3
        elif gm > 20:
            quality_pts += 2
        elif gm > 0:
            quality_pts += 1
    dr = latest.get("debt_ratio")
    if dr is not None:
        if dr < 40:
            quality_pts += 3
        elif dr < 60:
            quality_pts += 1
        elif dr > 75:
            quality_pts -= 2
    quality_pts = max(0, min(10, quality_pts))

    # -------- 估值 18:价值投资核心维度（PE + PB + PEG）----
    # 数据缺失时给中性分（不奖不罚），避免因数据缺失而把整个价值维度清零。
    # 旧实现是 PE/PB 任一缺失 → 该子项 0 分,导致价值维度永远 < 6 分,
    # 同时 VALUE_BUY 走不到(pe_band=None 不在低估档位),与页面"价值投资"定位冲突。
    value_pts = 0
    metrics: dict[str, Any] = {}
    pe = profile.get("pe")
    if pe is not None and pe > 0:
        if pe < 15:
            metrics["pe_band"] = "深度低估"
            value_pts += 10
        elif pe < 25:
            metrics["pe_band"] = "低估"
            value_pts += 8
        elif pe < 40:
            metrics["pe_band"] = "合理"
            value_pts += 5
        elif pe < 80:
            metrics["pe_band"] = "偏高"
            value_pts += 2
        else:
            metrics["pe_band"] = "高估"
        # PEG = PE / 净利同比。增速<=0 或 PE<=0 时无意义
        np_yoy_latest = latest.get("net_profit_yoy")
        if np_yoy_latest is not None and np_yoy_latest > 0:
            peg = pe / np_yoy_latest
            if peg < 0.5:
                value_pts += 4
                metrics["peg_band"] = "极低估"
            elif peg < 1:
                value_pts += 3
                metrics["peg_band"] = "低估"
            elif peg < 2:
                value_pts += 2
                metrics["peg_band"] = "合理"
            elif peg < 3:
                value_pts += 1
                metrics["peg_band"] = "偏高"
            else:
                metrics["peg_band"] = "高估"
        else:
            metrics["peg_band"] = "增速缺失"
    elif pe is not None and pe <= 0:
        # 亏损公司 PE 失效,给 2/18 中性偏负分(避免与正利润公司同等)
        value_pts += 2
        metrics["pe_band"] = "亏损"
    else:
        # PE 数据缺失(腾讯接口偶发空字段)→ 5/18 中性分
        # 不奖不罚,仅阻止整个价值维度被清零
        value_pts += 5
        metrics["pe_band"] = "数据缺失"

    pb = profile.get("pb")
    if pb is not None and pb > 0:
        if pb <= 1:
            metrics["pb_band"] = "深度低估"
            value_pts += 4
        elif pb < 1.5:
            metrics["pb_band"] = "低估"
            value_pts += 3
        elif pb < 3:
            metrics["pb_band"] = "合理"
            value_pts += 2
        elif pb < 6:
            metrics["pb_band"] = "偏高"
            value_pts += 1
        else:
            metrics["pb_band"] = "高估"
    else:
        # PB 数据缺失 → 1/18 中性分(PB 信息含量低于 PE)
        value_pts += 1
        metrics["pb_band"] = "数据缺失"

    # 连续正增长 + 估值合理/低估：再给点奖励（基本面+价值双正向）
    np_trend = [f.get("net_profit_yoy") for f in fin[:3] if f.get("net_profit_yoy") is not None]
    if np_trend and all(x > 0 for x in np_trend) and metrics.get("pe_band") in ("深度低估", "低估", "合理"):
        value_pts += 1
    value_pts = max(0, min(18, value_pts))

    # -------- 现金流 4:OCF/净利润（数据缺失时记 0,不扣分）
    ocf_pts = 0
    ocf_to_np = latest.get("ocf_to_netprofit")
    if ocf_to_np is not None:
        if ocf_to_np >= 1.0:
            ocf_pts = 4
            metrics["ocf_band"] = "优秀"
        elif ocf_to_np >= 0.5:
            ocf_pts = 3
            metrics["ocf_band"] = "健康"
        elif ocf_to_np >= 0:
            ocf_pts = 2
            metrics["ocf_band"] = "偏弱"
        else:
            metrics["ocf_band"] = "恶化"
    else:
        metrics["ocf_band"] = None

    # -------- 行业 6:板块强度
    industry_pts = 0
    b = profile.get("board") or ""
    strength = board_strength.get(b) if b else None
    if strength is not None:
        industry_pts = max(0, min(6, round(strength * 6)))
        metrics["industry_band"] = f"{strength:.2f}"
    else:
        metrics["industry_band"] = None

    total = growth_pts + quality_pts + value_pts + ocf_pts + industry_pts
    total = round(min(50, total), 1)
    detail = (f"成长{growth_pts}/12 质量{quality_pts}/10 估值{value_pts}/18 "
              f"现金流{ocf_pts}/4 行业{industry_pts}/6")
    completeness = 1 if (fin and has_valuation) else 0
    return {
        "score": total,
        "detail": detail,
        "completeness": completeness,
        "value_metrics": metrics,
    }


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

    资金信号强度按「流通市值占比」归一化：5 亿流入对 100 亿市值是 5%（强信号），
    对 1000 亿市值是 0.5%（弱信号）。有市值数据时按占比打分；市值缺失时退回
    绝对额（保持向后兼容）。
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
    total_mv = profile.get("total_mv")  # 总市值（亿）
    # 按市值占比打分（小数,如 0.05 = 5%）
    if total_mv and total_mv > 0:
        r1 = main_1 / (total_mv * 1e8)
        r3 = main_3 / (total_mv * 1e8)
        r5 = main_5 / (total_mv * 1e8)
        r30 = main_30 / (total_mv * 1e8)
        # 阈值：1 日 ±0.1%、3 日 ±0.3%、5 日 0.5%、30 日 1%
        if r1 > 0.001: pts += 3
        elif r1 < -0.001: pts -= 2
        if r3 > 0.003: pts += 3
        elif r3 < -0.003: pts -= 2
        if r5 > 0.005: pts += 2
        if r30 > 0.01: pts += 1
        # 资金拐点：30 日占比流出但 5 日占比流入 → 额外加分
        if r30 < -0.005 and r5 > 0.003:
            pts += 3
        detail = (
            f"1日{r1*100:+.2f}% 3日{r3*100:+.2f}% 5日{r5*100:+.2f}% "
            f"30日{r30*100:+.2f}%(占市值{int(total_mv)}亿)"
        )
    else:
        # 市值缺失 → 退回绝对额打分(兼容老数据)
        if main_1 > 0: pts += 3
        elif main_1 < 0: pts -= 2
        if main_3 > 0: pts += 3
        elif main_3 < 0: pts -= 2
        if main_5 > 0: pts += 2
        if main_30 > 0: pts += 1
        if main_30 < 0 and main_5 > 0:
            pts += 3
        detail = (
            f"1日{main_1/1e8:.1f}亿 3日{main_3/1e8:.1f}亿 "
            f"5日{main_5/1e8:.1f}亿 30日{main_30/1e8:.1f}亿"
        )
    pts = max(0, min(12, pts))
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
    """情绪妖股评分（满分 12）：连板高度 + 换手活跃度 + 涨停属性。

    连板 >= 6 视为「高位接盘」,与 _risk_score(+15)和 _buy_score(-8) 同向:
    不再奖励情绪溢价(给 0 而非 +6),避免三处评分互相打架。
    """
    pts = 0
    lb = profile.get("lianban") or 0
    if lb >= 6: pts += 0      # 高位接盘,不奖分(与风险/买点评分方向一致)
    elif lb >= 5: pts += 6
    elif lb >= 3: pts += 5
    elif lb >= 2: pts += 4
    elif lb >= 1: pts += 3
    to = profile.get("turnover")
    if to is not None:
        if 8 <= to <= 20: pts += 4   # 高换手
        elif 3 <= to < 8: pts += 2
        elif to > 25: pts += 1       # 过热但仍是焦点
    chg = profile.get("change_pct")
    # 连板 >= 6 时,涨幅加分也要打折(避免高位接盘再叠加情绪溢价)
    if chg is not None and chg > 5:
        pts += 2 if lb < 6 else 0
    pts = max(0, min(12, pts))
    note = "高位接盘" if lb >= 6 else f"连板{lb}"
    return {"score": pts, "detail": f"{note} 换手{to}%", "completeness": 1}


def _relative_score(profile: dict[str, Any]) -> dict[str, Any]:
    """个股相对板块强度（满分 8）：个股涨幅 − 所属板块平均涨幅。

    兑现策略文档中「个股强于板块」的承诺：同一热门板块里，跑赢板块均值的
    才是资金真正主攻的标的；跑输的往往是跟风补涨。
    """
    chg = profile.get("change_pct")
    board_avg = profile.get("board_avg_chg")
    board = profile.get("board") or ""
    if chg is None or board_avg is None:
        return {"score": 4, "detail": "板块强度数据缺失", "completeness": 0}
    diff = chg - board_avg
    if diff >= 5:
        pts = 8
    elif diff >= 3:
        pts = 7
    elif diff >= 1.5:
        pts = 6
    elif diff >= 0.5:
        pts = 5
    elif diff >= -1:
        pts = 4
    elif diff >= -3:
        pts = 2
    elif diff >= -6:
        pts = 1
    else:
        pts = 0
    return {
        "score": pts,
        "detail": f"相对板块 {diff:+.2f}%（个股 {chg:+.2f}% / 板块 {board_avg:+.2f}%）",
        "completeness": 1,
        "relative_chg": round(diff, 2),
    }


def _position_score(profile: dict[str, Any]) -> dict[str, Any]:
    """价格位置（满分 6）：当前价在近期高低区间中的百分位。

    自适应窗口:有 60 日数据用 60 日(对牛市中位 80% 仍判"高位"更准),否则
    回退 20 日。位置低（0-30%）= 回踩充分、上行空间大 → 高分；位置高
    (80-100%) = 接近阶段高点、追高风险 → 低分。中间位置给中性分,配合
    量价与资金维度共同决定买点。
    """
    kline = profile.get("kline") or []
    if len(kline) < 5:
        return {"score": 3, "detail": "K线数据不足", "completeness": 0}
    # 自适应窗口:60 日优先(更稳健),不足时回退 20 日
    if len(kline) >= 30:
        window = kline[-60:]
        win_label = "60日"
    else:
        window = kline[-20:]
        win_label = "20日"
    highs = [b.get("high") or b.get("close") for b in window if b.get("high") or b.get("close")]
    lows = [b.get("low") or b.get("close") for b in window if b.get("low") or b.get("close")]
    price = profile.get("price")
    if not highs or not lows or not price:
        return {"score": 3, "detail": "价格/区间缺失", "completeness": 0}
    hi, lo = max(highs), min(lows)
    # 关键：现价要参与区间计算。K 线最后一根常不含当日（数据源延迟/频控回落），
    # 而现价是实时的——涨停股现价会冲到 20 日区间之外，算出不合法的 >100% 位置。
    # 用现价扩展区间：创新高即 100%（最高位），破新低即 0%。
    hi = max(hi, price)
    lo = min(lo, price)
    if hi <= lo:
        return {"score": 3, "detail": "区间过窄", "completeness": 0}
    pos = max(0.0, min(100.0, (price - lo) / (hi - lo) * 100))  # 0=最低 100=最高
    if pos <= 20:
        pts = 6
    elif pos <= 40:
        pts = 5
    elif pos <= 60:
        pts = 4
    elif pos <= 80:
        pts = 2
    else:
        pts = 1
    return {
        "score": pts,
        "detail": f"{win_label}位置 {pos:.0f}%（{lo:.2f}~{hi:.2f}）",
        "completeness": 1,
        "position_pct": round(pos, 1),
        "position_window": win_label,
    }


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
    # 估值风险：PE / PB / PEG 异常
    pe = profile.get("pe")
    if pe is not None:
        if pe >= 200:
            risk += 15
            notes.append(f"PE {pe:.0f} 严重高估")
        elif pe > 100:
            risk += 10
            notes.append(f"PE {pe:.0f} 高估")
        elif pe > 80:
            risk += 5
            notes.append(f"PE {pe:.0f} 偏高")
        elif pe < 0:
            risk += 5
            notes.append("PE 亏损")
    pb = profile.get("pb")
    if pb is not None and pb > 10:
        risk += 8
        notes.append(f"PB {pb:.1f} 偏高")
    # OCF 持续恶化（若数据可得）
    fin = profile.get("financials") or []
    if len(fin) >= 2:
        ocf_curr = fin[0].get("ocf_to_netprofit")
        ocf_prev = fin[1].get("ocf_to_netprofit")
        if ocf_curr is not None and ocf_curr < 0:
            risk += 5
            notes.append("OCF 转负")
        if ocf_curr is not None and ocf_prev is not None and ocf_curr < -0.5 and ocf_prev < -0.5:
            risk += 5
            notes.append("OCF 连续恶化")
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
    # 价格位置：低位回踩加分、接近阶段高点减分（避免追高）
    pos = (scores.get("position") or {}).get("position_pct")
    if pos is not None:
        if pos <= 25: pts += 8      # 回踩充分，上行空间大
        elif pos <= 40: pts += 4
        elif pos >= 90: pts -= 12   # 20 日高点附近，追高风险大
        elif pos >= 75: pts -= 6
    # 相对板块强度：跑赢板块均值的才是资金主攻标的
    rel = (scores.get("relative") or {}).get("relative_chg")
    if rel is not None:
        if rel >= 3: pts += 8
        elif rel >= 1: pts += 4
        elif rel <= -3: pts -= 8
    # 估值时机：低估值是价值投资的最佳买点（资金面+技术面的补充维度）
    fin_score = scores.get("finance") or {}
    vm = fin_score.get("value_metrics") or {}
    pe_band = vm.get("pe_band")
    peg_band = vm.get("peg_band")
    if pe_band in ("深度低估", "低估"):
        pts += 12   # 估值底 + 资金/量价配合 → 黄金坑买点
    elif pe_band == "合理":
        pts += 4    # 估值合理即可介入
    elif pe_band == "偏高":
        pts -= 6    # 不再便宜,资金/量价需要非常强才考虑
    elif pe_band == "高估":
        pts -= 14   # 高估区,即使其它维度亮眼也要克制
    # PEG 极低估叠加分（即便 PE 正常,但增速匹配的好估值仍可加分）
    if peg_band == "极低估":
        pts += 4
    elif peg_band == "低估":
        pts += 2
    # 风险否决
    risk = scores.get("risk", {}).get("score", 0)
    if risk > 60: pts -= 40
    pts = max(0, min(100, round(pts)))
    pe_note = "估值" + str(pe_band) if pe_band else "估值未知"
    return {"score": pts, "detail": f"量价+资金+板块+情绪+{pe_note}"}


def _composite_score(scores: dict[str, Any], weights: dict[str, float]) -> float:
    """综合评分：各维度按相对权重做归一化加权平均，再减风险扣分。

    总分 = BASE_TOTAL × Σ(维度分×权重) / Σ(维度满分×权重)。
    权重表达「相对看重程度」：默认全 1.0 时即原始分之和（行为不变）；
    调大某维度，该维度强的股票总分上升、弱的下降。任意维度权重翻倍的
    相对影响相同（不再受各维度满分差异影响），总分恒在 0~BASE_TOTAL 之间。

    风险扣分非线性：risk > 20 时按 (risk-20)^1.5 / 12 扣分,接近 AVOID 阈值
    (60)时扣分显著加速,与「risk>60 → AVOID」信号语义一致。
    参考点:risk=30→2.6,risk=50→13.7,risk=60→21.1,risk=80→38.7。
    """
    m = valuecfg.DIM_MAXES
    num = sum(scores[k]["score"] * weights.get(k, 1.0) for k in m)
    den = sum(m[k] * weights.get(k, 1.0) for k in m)
    total = valuecfg.BASE_TOTAL * num / den if den > 0 else 0.0
    risk = scores["risk"]["score"]
    if risk > 20:
        # 非线性扣分:接近 AVOID 阈值时大幅扣分
        total -= (risk - 20) ** 1.5 / 12
    return max(0.0, min(float(valuecfg.BASE_TOTAL), round(total, 1)))


def _signal(
    profile: dict[str, Any], total: float, buy: int, risk: int,
    value_metrics: "dict[str, Any] | None" = None,
) -> str:
    """买卖信号：覆盖价值投资与情绪博弈两套语义：
        EXIT          高估 + 风险 → 建议清仓
        AVOID         风险 > 60    → 不参与
        VALUE_BUY     估值低估（PE/PEG 档位）+ 基本面稳健 + 风险低 → 价值低估买入
        QUALITY_HOLD  总分 80+ 且估值合理 → 长期持有
        BREAKOUT_BUY  趋势突破 + 量价齐升
        PULLBACK_BUY  启动期/分歧期低吸
        BUY           综合达标
        WATCH         60~74 尚需确认
        REDUCE        短线急跌 -4%+
    """
    chg = profile.get("change_pct") or 0
    vr = profile.get("volume_ratio") or 0
    lb = profile.get("lianban") or 0
    pe = profile.get("pe")
    # 阈值按 BASE_TOTAL 的相对比例（维度增减后 BASE_TOTAL 会变，写死会漂移）：
    #   hi ≈ 0.815（原 75/92）、top ≈ 0.87（原 80/92）、mid ≈ 0.65（原 60/92）
    base = valuecfg.BASE_TOTAL or 92.0
    hi_cut = base * 0.815
    top_cut = base * 0.87
    mid_cut = base * 0.65
    if risk > 60:
        return "AVOID"
    # 价值投资维度（独立分支,不依赖 total>=hi_cut）：
    # 旧口径硬性要求 PE<15 过严，会漏掉「PE 30 但增速 40%」这类 PEG 极优标的。
    # 改为按估值档位判定：PE 低估/深度低估 或 PEG 低估/极低估，配基本面与低风险。
    vm = value_metrics or {}
    pe_band = vm.get("pe_band")
    peg_band = vm.get("peg_band")
    value_ok = pe_band in ("深度低估", "低估") or peg_band in ("极低估", "低估")
    # 连板梯队股走情绪线，不占用价值买点语义
    if value_ok and total >= mid_cut and risk < 30 and lb == 0:
        return "VALUE_BUY"
    # EXIT：估值严重高估 + 风险中等 → 建议清仓
    if pe is not None and pe >= 100 and risk > 25:
        return "EXIT"
    if total >= hi_cut and buy >= 70:
        # 长期持有：总分优秀 + 估值明确合理（PE 缺失时不走 QUALITY_HOLD,保持既有信号）
        if total >= top_cut and pe is not None and 0 < pe <= 30:
            return "QUALITY_HOLD"
        if chg > 5 and vr >= 1.5 and lb >= 1:
            return "BREAKOUT_BUY"
        if 0 < chg <= 5 and vr < 1.2 and lb >= 1:
            return "PULLBACK_BUY"
        return "BUY"
    if total >= mid_cut:
        return "WATCH"
    if chg < -4:
        return "REDUCE"
    return "AVOID"


def _grade(total: float) -> tuple[str, str]:
    """分级：阈值按 BASE_TOTAL 的相对比例判定。

    维度增减会改变 BASE_TOTAL（如新增相对强度/价格位置后 92 → 106），
    写死绝对阈值会让分布整体漂移，故统一用占比口径，保证分级语义稳定。
    """
    ratio = total / valuecfg.BASE_TOTAL if valuecfg.BASE_TOTAL else 0.0
    if ratio >= 0.92: return "S", "核心机会池"
    if ratio >= 0.85: return "A", "重点观察池"
    if ratio >= 0.76: return "B", "待确认池"
    if ratio >= 0.65: return "C", "观察池"
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
    cand: dict[str, Any], board_strength: dict[str, float],
    weights: dict[str, float] | None = None,
    board_avg: "dict[str, float] | None" = None,
    extra: "dict[str, Any] | None" = None,
) -> dict[str, Any] | None:
    profile = await _stock_profile(cand, extra=extra)
    if profile.get("price") is None and not profile.get("financials"):
        return None  # 核心数据全缺，跳过
    # 注入所属板块平均涨幅，供「个股相对板块强度」评分使用
    b = profile.get("board") or ""
    if board_avg and b:
        profile["board_avg_chg"] = board_avg.get(b)
    fin_score = _financial_score(profile, board_strength=board_strength)
    board = _board_score(profile, board_strength)
    flow = _flow_score(profile)
    volume = _volume_score(profile)
    emotion = _emotion_score(profile)
    relative = _relative_score(profile)
    position = _position_score(profile)
    risk = _risk_score(profile, fin_score)
    scores = {"finance": fin_score, "board": board, "flow": flow,
              "volume": volume, "emotion": emotion,
              "relative": relative, "position": position, "risk": risk}
    # 综合评分：各维度加权（默认权重 1.0 即原始分）- 风险扣分
    w = weights or valuecfg.get_weights()
    total = _composite_score(scores, w)
    buy = _buy_score(profile, scores)
    trade = round(total * 0.7 + buy["score"] * 0.3, 1)
    grade, grade_name = _grade(total)
    completeness = _completeness(profile)
    signal = _signal(profile, total, buy["score"], risk["score"],
                     fin_score.get("value_metrics") or {})
    # 投资建议 = 信号的中文含义,前端直接展示
    advice_map = {
        "EXIT": "建议清仓",
        "AVOID": "不参与",
        "VALUE_BUY": "价值低估买入",
        "QUALITY_HOLD": "建议长期持有",
        "BREAKOUT_BUY": "突破买入",
        "PULLBACK_BUY": "分歧低吸",
        "BUY": "建议买入",
        "WATCH": "观察确认",
        "REDUCE": "建议减仓",
    }
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
        "value_metrics": fin_score.get("value_metrics", {}),
        # 新增：相对板块超额收益与 20 日价格位置（前端展示 + 买点提示）
        "relative_chg": relative.get("relative_chg"),
        "position_pct": position.get("position_pct"),
        "board_avg_chg": profile.get("board_avg_chg"),
        "risk_notes": risk.get("notes", []),
        "total_score": total, "buy_score": buy["score"], "trade_score": trade,
        "grade": grade, "grade_name": grade_name,
        "signal": signal, "advice": advice_map.get(signal, "—"),
        "completeness": completeness,
    }


# ------------------------------------------------------------------ 板块强度

def _board_avg_change(
    zt_rows: list[dict[str, Any]], hot_rows: list[dict[str, Any]]
) -> dict[str, float]:
    """各板块当日平均涨幅（涨停池去重后叠加热门榜），用于个股相对强度对比。

    涨停池个股涨幅几乎都是 ±10%/±20%（封板），直接用会把板块均值顶到极端值；
    因此优先取热门榜（连续分布）的均值，涨停池不足时再回退。
    """
    agg: dict[str, list[float]] = {}
    for r in hot_rows:
        b = r.get("board") or ""
        c = r.get("change_pct")
        if b and c is not None:
            agg.setdefault(b, []).append(c)
    for r in zt_rows:
        b = r.get("board") or ""
        c = r.get("change_pct")
        if b and c is not None:
            agg.setdefault(b, []).append(c)
    return {
        b: round(sum(v) / len(v), 2)
        for b, v in agg.items() if v
    }


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
    """完整选股流程（聚合缓存 15 分钟，权重变化自动作废缓存）。"""
    key = f"value:screen:{valuecfg.fingerprint()}"
    if not force:
        cached = _cache.peek(key)
        if cached:
            return cached

    weights = valuecfg.get_weights()

    # 第一层：市场环境（指数 / 涨停池 / 炸板池 / 热门榜 并发）
    indices, zt, zb, hot = await asyncio.gather(
        _fetch_index_quotes(), _fetch_zt_pool(), _fetch_zb_pool(), _fetch_hot_pool(20)
    )
    avg_chg = 0.0
    if hot:
        chgs = [h.get("change_pct") for h in hot if h.get("change_pct") is not None]
        avg_chg = sum(chgs) / len(chgs) if chgs else 0.0
    market = _market_state(indices, zt.get("count") or 0, avg_chg,
                           zb_count=zb.get("count") or 0)

    # 板块强度
    board_strength = await _fetch_board_strength(zt.get("rows") or [])

    # 候选池
    candidates = _merge_candidates(zt, hot)
    log.info("价值选股：市场=%s(%s) 涨停=%s 炸板=%s(%.0f%%) 候选=%s",
             market["name"], market["state"], zt.get("count"),
             zb.get("count") or 0, market.get("zb_rate") or 0, len(candidates))

    # 板块平均涨幅（涨停池 + 热门榜聚合），供「个股相对板块强度」评分
    board_avg = _board_avg_change(zt.get("rows") or [], hot)

    # 腾讯补充字段一次批量取完，避免逐只请求（40 只候选 40 次 → 1 次）
    extra_map = await _tencent_extra_batch(candidates)

    # 逐股评分（并发，限流友好）
    sem = asyncio.Semaphore(8)
    async def _limited(c: dict[str, Any]):
        async with sem:
            return await _analyze_one(
                c, board_strength, weights,
                board_avg=board_avg, extra=extra_map.get(f"{c['code']}.{c['market']}"),
            )
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
    # 趋势池阈值同样按 BASE_TOTAL 占比（约 60%），避免维度增减后分池漂移。
    # 不再叠加 grade 过滤：grade 本身就由 total_score 推导，双重门槛（0.60 与
    # grade C 的 0.65）会让 0.60~0.65 区间的股票被误筛掉，导致分池为空。
    trend_cut = round(valuecfg.BASE_TOTAL * 0.60, 1)
    pool_trend = sorted(
        [s for s in stocks if s["total_score"] >= trend_cut],
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
            "zb_count": zb.get("count") or 0,
            "zb_rate": market.get("zb_rate") or 0,
            "candidate_count": len(candidates),
        },
        "board_top": [{"name": b, "strength": s} for b, s in board_top],
        "pools": {
            "core": pool_core,
            "trend": pool_trend,
            "emotion": pool_emotion,
        },
        "stocks": stocks,
        "weights": weights,
        "session": __import__("backend.service", fromlist=["session_info"]).session_info(),
    }
    _cache.put(key, result, _CACHE_TTL)
    return result
