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
import time
from datetime import datetime
from typing import Any

from . import cache as cache_mod, service, valuecfg, zt_pool
from .config import settings

_cache = cache_mod.cache
from .providers import ProviderError, registry
from .providers.base import fetch
from .utils import to_float

log = logging.getLogger("value_screener")

_CACHE_TTL = 900.0  # 15 分钟聚合缓存（避免反复打接口）
_CANDIDATE_LIMIT = 40  # 候选池总席位（涨停池 + 热门榜 + 价值基准 去重）
_EMOTION_QUOTA = 24  # 情绪赛道（涨停池+热门榜）固定配额；基准池保底 40-24=16 席
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
    """东财涨停池（含连板/板块/换手）——委托 zt_pool 共享模块（与首页共用缓存）。"""
    return await zt_pool.get_zt_pool()


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

    配额分流（P1 修复）：情绪赛道与价值基准是两个策略诉求，共用一个截断
    名额时，正常交易日涨停池+热门榜就能占满 40 席，40 只蓝筹根本进不了
    候选，「价值投资」页实际跑的是情绪选股。现拆成固定配额：
      - 情绪赛道（涨停池→热门榜顺序）最多 _EMOTION_QUOTA=24 席;
      - 价值基准池保底补位到总量 _CANDIDATE_LIMIT=40（情绪满额时恰得
        40-24=16 席；弱势市情绪池不满时基准池自动多补，总席位不浪费）。
    """
    emotion: dict[str, dict[str, Any]] = {}
    for r in zt.get("rows") or []:
        if not _is_stock_code(r["code"], r["market"]):
            continue
        emotion[f"{r['code']}.{r['market']}"] = r
    for r in hot:
        if not _is_stock_code(r["code"], r["market"]):
            continue
        key = f"{r['code']}.{r['market']}"
        if key not in emotion:
            emotion[key] = r
    out = list(emotion.values())[:_EMOTION_QUOTA]
    seen = {f"{r['code']}.{r['market']}" for r in out}
    # 价值基准池：去重后补位（基准股若已进情绪池，视为已占 1 席，不重复抓数据）
    for r in _VALUE_BASELINE:
        if len(out) >= _CANDIDATE_LIMIT:
            break
        if not _is_stock_code(r["code"], r["market"]):
            continue
        key = f"{r['code']}.{r['market']}"
        if key in seen:
            continue
        # 补齐 hot 需要的字段,后续 _stock_profile 会用真实数据覆盖
        out.append({**r, "change_pct": 0.0, "turnover": None,
                    "volume_ratio": None, "lianban": 0, "board": ""})
        seen.add(key)
    return out[:_CANDIDATE_LIMIT]


# ------------------------------------------------------------------ 单股数据

# 腾讯 qt.gtimg.cn 原始字段索引（实测核对，勿凭记忆改）：
#   f[30]=行情时间(YYYYMMDDHHMMSS) f[37]=成交额(万) f[38]=换手率%
#   f[39]=PE(TTM)                  f[43]=振幅%      f[44]=流通市值(亿) f[45]=总市值(亿)
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
        "float_mv": _f(44),       # 流通市值（亿）；机会投资妖股「市值弹性」依赖
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


# ------------------------------------------------------------------ 板块资金流 / 实时资金（push2delay）

async def _fetch_board_flow(limit: int = 100) -> dict[str, dict[str, Any]]:
    """东财板块资金流榜（行业口径 m:90 t:2，push2delay 实测可用）。

    字段（实测核对，勿凭记忆改）：
      f12=板块代码(BKxxxx) f14=板块名 f3=板块涨跌幅% f62=今日主力净流入(元)
      f164=5日主力净流入(元) f204/f205=领涨股名/代码 f206=领涨股涨跌幅%
    数值列统一 to_float：上游对无数据返回字符串 "-" 而非 null。
    失败返回 {} → 板块资金项按缺失跳过（不奖不罚），不阻塞主流程。
    """
    url = ("https://push2delay.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2&fid=f62"
           "&fs=m%%3A90%%20t%%3A2&fields=f12,f14,f3,f62,f164,f204,f205,f206" % limit)
    try:
        resp = await fetch(url, headers={"Referer": "https://quote.eastmoney.com/"})
        data = (resp.json() or {}).get("data") or {}
        out: dict[str, dict[str, Any]] = {}
        for i, r in enumerate(data.get("diff") or []):
            name = str(r.get("f14") or "")
            if not name:
                continue
            # 领涨股盘前/无数据时上游返回 "-"，与缺失同义
            ln = str(r.get("f204") or "").strip()
            leader_name = ln if ln not in ("", "-") else None
            out[name] = {
                "name": name,
                "bk_code": r.get("f12") or "",
                "chg": to_float(r.get("f3")),           # 板块涨跌幅 %
                "main_today": to_float(r.get("f62")),   # 今日主力净流入（元）
                "main_5d": to_float(r.get("f164")),     # 5日主力净流入（元）
                "leader_name": leader_name,             # 领涨股名
                "leader_code": (r.get("f205") or None) if leader_name else None,
                "leader_chg": to_float(r.get("f206")),  # 领涨股涨跌幅 %
                "rank": i + 1,                # 按今日主力净流入降序的名次
            }
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("板块资金流获取失败：%s", exc)
        return {}


async def board_flow_leaders(limit: int = 50) -> list[dict[str, Any]]:
    """板块资金流龙头下钻（热点追踪 P1-2 公开包装）。

    复用 _fetch_board_flow（内部已做 "-" → None 清洗），拍平为按板块逐条的
    leaders 列表：[{board, code, name, pct_chg, main_net, leader_name,
    leader_code, leader_pct}]。上游拉取失败时 _fetch_board_flow 返回 {} →
    本函数静默返回 []，不阻塞热点聚合主流程。
    """
    flow = await _fetch_board_flow(limit)
    return [
        {
            "board": b.get("name") or name,
            "code": b.get("bk_code") or "",
            "name": b.get("name") or name,
            "pct_chg": b.get("chg"),
            "main_net": b.get("main_today"),
            "leader_name": b.get("leader_name"),
            "leader_code": b.get("leader_code"),
            "leader_pct": b.get("leader_chg"),
        }
        for name, b in flow.items()
    ]


async def _fetch_main_inflow(cands: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """push2delay 批量实时资金：f62 主净 / f184 净比 / f8 换手 / f10 量比。

    背景：push2 主站频控时 registry().quotes 会自动切腾讯源，而腾讯 Quote
    缺主力资金字段。push2delay 同接口可用（延迟约 1 分钟，日内确认口径可接受）。
    失败返回 {} 不阻塞；个股级字段仅非 None 值被消费方采纳。
    """
    if not cands:
        return {}
    secids = ",".join(("1" if c["market"] == "SH" else "0") + "." + c["code"]
                      for c in cands)
    try:
        resp = await fetch(
            "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
            headers={"Referer": "https://quote.eastmoney.com/"},
            params={"fltt": 2, "invt": 2, "fields": "f12,f8,f10,f62,f184",
                    "secids": secids, "ut": "fa5fd1943c7b386f172d6893dbfba10b"},
        )
        rows = ((resp.json() or {}).get("data") or {}).get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            code = str(r.get("f12") or "")
            if len(code) != 6:
                continue
            out[code] = {
                "main_net_inflow": to_float(r.get("f62")),
                "main_net_pct": to_float(r.get("f184")),
                "turnover": to_float(r.get("f8")),
                "volume_ratio": to_float(r.get("f10")),
            }
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("候选实时资金获取失败：%s", exc)
        return {}


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
    except Exception as exc:  # noqa: BLE001
        log.warning("%s 异常，按空数据继续: %s", "_stock_profile", exc)
        pass
    # 腾讯补充字段：优先用预取的批量结果，缺失时才单独补一次
    if extra is None:
        extra = await _tencent_extra(code, market)
    profile.update(extra or {})

    async def _fin() -> list[dict[str, Any]]:
        try:
            # 走 service.financials_cached（详情页 financials 缓存，6h TTL）。
            # 取 24 期（约 6 年）：近 5 个完整年报期用于 ROE 趋势（P2 财务深度），
            # 评分函数内部自行按 FY 切片，其余消费方不受影响
            fin = await service.financials_cached(code, market, 24)
            return [
                {
                    "period": f.period, "revenue_yoy": f.revenue_yoy,
                    "net_profit_yoy": f.net_profit_yoy,
                    "net_profit": f.net_profit, "roe": f.roe,
                    # P2 财务深度：扣非净利及同比（两路 provider 均有，此前转换层丢失）
                    "net_profit_deduct": f.net_profit_deduct,
                    "net_profit_deduct_yoy": f.net_profit_deduct_yoy,
                    "gross_margin": f.gross_margin, "debt_ratio": f.debt_ratio,
                    "ocf_to_netprofit": getattr(f, "ocf_to_netprofit", None),
                }
                for f in fin
            ]
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 异常，按空数据继续: %s", "_fin", exc)
            return []

    async def _flow() -> list[dict[str, Any]]:
        try:
            # 走 service.flow_cached（详情页 flow 缓存，历史 TTL）；源失败时空列表兜底
            flow = await service.flow_cached(code, market, 30)
            return [{"date": d.date, "main": d.main, "main_pct": d.main_pct} for d in flow]
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 异常，按空数据继续: %s", "_flow", exc)
            return []

    async def _kline() -> list[dict[str, Any]]:
        try:
            # 60 日 K 线:既给 20 日价格位置评分用,又给位置自适应(60 日窗口)用。
            # 走 service.kline_cached（详情页 kline 缓存，KLINE_LIMIT=260 根里切最近 60 根）
            bars = await service.kline_cached(code, market, 60)
            return [
                {"date": b.date, "close": b.close, "change_pct": b.change_pct,
                 "volume": b.volume, "high": b.high, "low": b.low}
                for b in bars
            ]
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 异常，按空数据继续: %s", "_kline", exc)
            return []

    # 三路并发：财务 / 资金 / K线互不依赖
    fin_list, flow_list, kline_list = await asyncio.gather(_fin(), _flow(), _kline())
    profile["financials"] = fin_list
    profile["flow"] = flow_list
    profile["kline"] = kline_list
    return profile


# ------------------------------------------------------------------ 评分引擎
# 已拆分至 value_scores.py（体检 M7：原文件 1788 行超载）；此处 re-export
# 保持函数名与 import 路径兼容，_analyze_one 等调用方无需改动。
from .value_scores import (  # noqa: F401  re-export
    _INDUSTRY_PE_TOLERANCE,
    _INDUSTRY_PE_TOLERANCE_DEFAULT,
    _SIGNAL_TRIGGERS,
    _TRADE_W_BUY,
    _TRADE_W_TOTAL,
    _advice_for,
    _board_score,
    _buy_score,
    _completeness,
    _composite_score,
    _emotion_score,
    _financial_score,
    _flow_score,
    _grade,
    _industry_pe_tolerance,
    _position_score,
    _relative_score,
    _risk_score,
    _signal,
    _volume_score,
)
async def _analyze_one(
    cand: dict[str, Any], board_strength: dict[str, float],
    weights: dict[str, float] | None = None,
    board_avg: "dict[str, float] | None" = None,
    extra: "dict[str, Any] | None" = None,
    risk_extra: "dict[str, Any] | None" = None,
    board_flow: "dict[str, dict[str, Any]] | None" = None,
    realtime: "dict[str, Any] | None" = None,
) -> dict[str, Any] | None:
    profile = await _stock_profile(cand, extra=extra)
    if profile.get("price") is None and not profile.get("financials"):
        return None  # 核心数据全缺，跳过
    # P2 暴雷过滤字段（run_screen 批量预取，无数据时为空 dict → 各项静默跳过）
    if risk_extra:
        profile["risk_extras"] = risk_extra
    # 盘中实时主净确认（push2delay 批量，无数据时 None → 资金评分按日级口径）
    if realtime:
        profile["realtime"] = realtime
    # 注入所属板块平均涨幅，供「个股相对板块强度」评分使用
    b = profile.get("board") or ""
    if board_avg and b:
        profile["board_avg_chg"] = board_avg.get(b)
    fin_score = _financial_score(profile, board_strength=board_strength)
    board = _board_score(profile, board_strength, board_flow)
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
    # trade_score 排序分：以综合评分（吃 7 维用户权重）为主，择时买点分为辅。
    # 比例见模块级常量 _TRADE_W_TOTAL / _TRADE_W_BUY（当前 0.85/0.15）。
    trade = round(total * _TRADE_W_TOTAL + buy["score"] * _TRADE_W_BUY, 1)
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
    signal, advice, trigger = _advice_for(
        signal, risk["score"], advice_map)
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
        "signal": signal, "advice": advice,
        "signal_trigger": trigger,
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


def _hot_board_strength(hot_rows: list[dict[str, Any]]) -> dict[str, float]:
    """热门榜兜底板块强度：涨停池拿不到的板块（银行/白酒等蓝筹少有涨停）用它补齐。

    热门≠涨停：计数权重降到 40%（15 只封顶）、涨幅权重 60%（6% 封顶），
    且整体上限压到 0.8，避免与真实涨停强度混淆。
    """
    agg: dict[str, list[float]] = {}
    for r in hot_rows:
        b = r.get("board") or ""
        c = r.get("change_pct")
        if b and c is not None:
            agg.setdefault(b, []).append(c)
    strength: dict[str, float] = {}
    for b, chgs in agg.items():
        count = len(chgs)
        avg = sum(chgs) / count if chgs else 0.0
        s = min(1.0, count / 15) * 0.4 + min(1.0, max(0.0, avg) / 6) * 0.6
        strength[b] = round(min(0.8, s), 3)
    return strength


async def _fetch_board_strength(
    zt_rows: list[dict[str, Any]],
    hot_rows: "list[dict[str, Any]] | None" = None,
) -> dict[str, float]:
    """板块强度：主源为涨停池聚合；涨停池覆盖不到的板块用热门榜均值兜底。

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
    # P2 兜底：涨停池为空的板块（蓝筹）用热门榜均值补齐，setdefault 不覆盖涨停池结果
    if hot_rows:
        for b, s in _hot_board_strength(hot_rows).items():
            strength.setdefault(b, s)
    return strength


# ------------------------------------------------------------------ 暴雷过滤数据源
# 东财 datacenter 三接口，2026-09-01 curl 实测通过（601179.SH 返回有效数据）：
#   商誉  RPT_GOODWILL_STOCKDETAILS  → GOODWILL / SUMSHEQUITY_RATIO（商誉÷归母净资产，小数）
#   质押  RPT_CSDC_LIST              → PLEDGE_RATIO（%，中登周频；止于清零年属正常）
#   解禁  RPT_LIFT_STAGE             → FREE_DATE / LIFT_MARKET_CAP（元）/ 解禁类型
# 审计意见接口（RPT_F10_FINANCE_AUDITOPINION 等 4 个 reportName）实测均「报表配置不存在」，
# 数据源不可用，按项目原则不接入、不编造。
_DC_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_DC_HEADERS = {"Referer": "https://data.eastmoney.com/"}
_RISK_EXTRA_TTL = 12 * 3600.0  # 商誉/质押/解禁均为低频数据（季报/周频/预告制），12h 缓存
_risk_extra_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _dc_rows(report: str, filt: str, columns: str, sort: str,
                   order: str = "-1") -> list[dict[str, Any]]:
    """datacenter 单报表查询；空结果（code 9201）/解析失败 → 空列表。"""
    try:
        resp = await fetch(
            _DC_BASE, headers=_DC_HEADERS,
            params={
                "reportName": report, "columns": columns, "filter": filt,
                "sortColumns": sort, "sortTypes": order,
                "pageSize": 10, "pageNumber": 1,
                "source": "WEB", "client": "WEB",
            },
        )
        result = (resp.json() or {}).get("result") or {}
        return result.get("data") or []
    except Exception as exc:  # noqa: BLE001 — 单源失败静默跳过，不阻断选股
        log.debug("datacenter %s 查询失败: %s", report, exc)
        return []


async def _risk_extra_one(code: str) -> dict[str, Any]:
    """单只股票的暴雷过滤字段（商誉占比 / 质押比例 / 90 日内解禁）。全部尽力而为。"""
    out: dict[str, Any] = {}
    code_filter = f'(SECURITY_CODE="{code}")'
    # 商誉：最新报告期（REPORT_DATE 倒序取第一条）
    rows = await _dc_rows("RPT_GOODWILL_STOCKDETAILS", code_filter,
                          "SECURITY_CODE,REPORT_DATE,GOODWILL,SUMSHEQUITY_RATIO",
                          "REPORT_DATE")
    if rows and rows[0].get("SUMSHEQUITY_RATIO") is not None:
        out["goodwill_ratio"] = rows[0]["SUMSHEQUITY_RATIO"]
    # 质押：最新一周（TRADE_DATE 倒序取第一条）
    rows = await _dc_rows("RPT_CSDC_LIST", code_filter,
                          "SECURITY_CODE,TRADE_DATE,PLEDGE_RATIO", "TRADE_DATE")
    if rows and rows[0].get("PLEDGE_RATIO") is not None:
        out["pledge_ratio"] = rows[0]["PLEDGE_RATIO"]
    # 解禁：未来 90 日内合计市值（元）+ 最近一次解禁倒计时天数（FREE_DATE 正序）
    today = datetime.now().strftime("%Y-%m-%d")
    rows = await _dc_rows(
        "RPT_LIFT_STAGE",
        f'{code_filter}(FREE_DATE>=\'{today}\')',
        "SECURITY_CODE,FREE_DATE,LIFT_MARKET_CAP,FREE_SHARES_TYPE",
        "FREE_DATE", order="1",
    )
    now = datetime.now()
    total_cap = 0.0
    nearest_days: int | None = None
    for r in rows:
        try:
            free_date = datetime.strptime(str(r.get("FREE_DATE") or "")[:10], "%Y-%m-%d")
        except ValueError:
            continue
        days = (free_date - now).days
        if days > 90:
            break
        total_cap += float(r.get("LIFT_MARKET_CAP") or 0)
        if nearest_days is None:
            nearest_days = max(0, days)
    if total_cap > 0 and nearest_days is not None:
        out["lift_market_cap"] = total_cap
        out["lift_days"] = nearest_days
    return out


async def _fetch_risk_extras(cands: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """批量预取候选股暴雷过滤字段，key 为 `code.market`。

    进程内 12h TTL 缓存 + 信号量限流（40 候选 × 3 端点 = 120 请求，
    首轮约 10s，命中缓存后接近 0）。
    """
    sem = asyncio.Semaphore(6)

    async def _one(c: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        key = f"{c['code']}.{c['market']}"
        hit = _risk_extra_cache.get(key)
        if hit and time.time() - hit[0] < _RISK_EXTRA_TTL:
            return key, hit[1]
        async with sem:
            try:
                data = await _risk_extra_one(c["code"])
            except Exception as exc:  # noqa: BLE001
                log.warning("risk_extras %s 失败，按空数据继续: %s", key, exc)
                data = {}
        _risk_extra_cache[key] = (time.time(), data)
        return key, data

    pairs = await asyncio.gather(*[_one(c) for c in cands])
    return dict(pairs)


# ------------------------------------------------------------------ 对外入口

async def run_screen(force: bool = False) -> dict[str, Any]:
    """完整选股流程（聚合缓存 15 分钟，权重变化自动作废缓存）。"""
    key = f"value:screen:{valuecfg.fingerprint()}"
    if not force:
        cached = _cache.peek(key)
        if cached:
            return cached

    weights = valuecfg.get_weights()

    # 第一层：市场环境（指数 / 涨停池 / 炸板池 / 热门榜 / 板块资金流 并发）
    indices, zt, zb, hot, board_flow = await asyncio.gather(
        _fetch_index_quotes(), _fetch_zt_pool(), _fetch_zb_pool(),
        _fetch_hot_pool(20), _fetch_board_flow(),
    )
    avg_chg = 0.0
    if hot:
        chgs = [h.get("change_pct") for h in hot if h.get("change_pct") is not None]
        avg_chg = sum(chgs) / len(chgs) if chgs else 0.0
    market = _market_state(indices, zt.get("count") or 0, avg_chg,
                           zb_count=zb.get("count") or 0)

    # 板块强度（涨停池聚合 + 热门榜兜底）
    board_strength = await _fetch_board_strength(zt.get("rows") or [], hot)

    # 候选池
    candidates = _merge_candidates(zt, hot)
    log.info("价值选股：市场=%s(%s) 涨停=%s 炸板=%s(%.0f%%) 候选=%s",
             market["name"], market["state"], zt.get("count"),
             zb.get("count") or 0, market.get("zb_rate") or 0, len(candidates))

    # 板块平均涨幅（涨停池 + 热门榜聚合），供「个股相对板块强度」评分
    board_avg = _board_avg_change(zt.get("rows") or [], hot)

    # 腾讯补充字段一次批量取完，避免逐只请求（40 只候选 40 次 → 1 次）
    extra_map = await _tencent_extra_batch(candidates)

    # P2 暴雷过滤字段（商誉/质押/解禁）批量预取，12h 进程内缓存
    risk_map = await _fetch_risk_extras(candidates)

    # 盘中实时主净确认（push2delay 批量，延迟约 1 分钟；失败为 {} 按日级口径）
    realtime_map = await _fetch_main_inflow(candidates)

    # 逐股评分（并发，限流友好）
    sem = asyncio.Semaphore(8)
    async def _limited(c: dict[str, Any]):
        async with sem:
            return await _analyze_one(
                c, board_strength, weights,
                board_avg=board_avg, extra=extra_map.get(f"{c['code']}.{c['market']}"),
                risk_extra=risk_map.get(f"{c['code']}.{c['market']}"),
                board_flow=board_flow,
                realtime=realtime_map.get(c["code"]),
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
    # 趋势池阈值按 BASE_TOTAL 占比(0.55),低于 C 档(0.60),确保趋势池不为空。
    # 双重门槛会让 0.55~0.60 区间的优质股被误筛掉,这里只用单层 total 过滤。
    trend_cut = round(valuecfg.BASE_TOTAL * 0.55, 1)
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
            # 板块维度就绪标志：涨停池与板块强度全空（盘前/数据未更新）时
            # 板块维度按中性计，评分普遍偏低属正常，前端据此刻画提示条
            "board_ready": bool(board_strength) or bool(zt.get("count")),
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
