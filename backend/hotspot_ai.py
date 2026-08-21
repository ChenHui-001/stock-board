"""热点快讯 AI 分析：单条市场快讯 → 利好/利空行业 + 关联度最高的三只股票。

链路：
1. LLM 可用时：把快讯交给大模型，让它输出整体情绪 + 利好/利空/关注行业 + 检索关键词
   （关键词用于在真实 A 股市场检索关联股票，避免模型编造代码）。
2. LLM 不可用/失败：内置行业词典匹配快讯文本识别行业，情绪用关键词规则判定
   （与个股资讯解读同口径）。
3. 无论哪条路径，关联股票都通过 registry().search() 用真实搜索接口解析，
   保证返回的每只股票代码/名称都是真实存在的，并按「命中关键词数 + 检索顺序」排序。

结果按快讯标题+摘要指纹缓存（默认 10 分钟），避免重复打 LLM 与搜索接口；
cache.get_or_set 自带单飞去重，同一快讯并发点击只执行一次分析。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from . import llm, news, service, storage
from .cache import cache
from .providers import registry
from .utils import describe_exc, full_code, now

log = logging.getLogger("hotspot_ai")

TTL = 600.0  # 单条快讯分析结果缓存（秒）

# 规则路径行业词典：行业名 -> 匹配/检索关键词。
# 关键词同时用于（a）匹配快讯文本识别行业，（b）检索真实 A 股关联股票。
_SECTORS: list[tuple[str, tuple[str, ...]]] = [
    ("新能源", ("新能源", "光伏", "风电", "储能", "氢能", "锂电", "电池")),
    ("光伏", ("光伏", "硅料", "组件", "逆变器", "HJT", "TOPCon")),
    ("锂电池", ("锂电池", "锂电", "正极", "负极", "隔膜", "电解液", "碳酸锂")),
    ("储能", ("储能", "电化学储能", "抽水蓄能")),
    ("半导体/芯片", ("半导体", "芯片", "集成电路", "晶圆", "光刻", "封测")),
    ("半导体设备", ("半导体设备", "光刻机", "刻蚀")),
    ("人工智能", ("人工智能", "AI大模型", "大模型", "算力", "AIGC", "机器人")),
    ("算力", ("算力", "数据中心", "服务器", "液冷")),
    ("机器人", ("机器人", "人形机器人", "减速器", "伺服")),
    ("医药", ("医药", "创新药", "疫苗", "CXO", "医疗器械", "生物医药")),
    ("创新药", ("创新药", "GLP-1", "ADC", "双抗")),
    ("医疗器械", ("医疗器械", "医疗设备", "耗材")),
    ("白酒", ("白酒", "茅台", "五粮液", "酿酒", "啤酒")),
    ("地产", ("房地产", "地产", "楼市", "房价", "土拍")),
    ("银行", ("银行", "信贷", "降息", "LPR", "存贷款")),
    ("券商", ("券商", "证券", "投行", "资本市场", "经纪")),
    ("保险", ("保险", "寿险", "财险", "保费")),
    ("军工", ("军工", "国防", "航天", "航空", "导弹")),
    ("卫星互联网", ("卫星互联网", "卫星", "北斗", "商业航天")),
    ("汽车", ("汽车", "新能源车", "整车", "智能驾驶", "汽车零部件")),
    ("低空经济", ("低空经济", "eVTOL", "无人机", "飞行汽车")),
    ("消费", ("消费", "零售", "电商", "免税", "家电")),
    ("家电", ("家电", "空调", "白电", "小家电")),
    ("黄金", ("黄金", "金价", "贵金属")),
    ("煤炭", ("煤炭", "煤价", "焦煤", "动力煤")),
    ("石油石化", ("石油", "原油", "油气", "油价", "炼化")),
    ("有色金属", ("有色", "铜", "铝", "稀土", "锂矿", "镍")),
    ("农业", ("农业", "粮食", "种业", "猪肉", "养殖", "饲料")),
    ("基建", ("基建", "工程", "建筑", "水泥", "装配式")),
    ("传媒", ("传媒", "影视", "游戏", "广告", "出版")),
    ("游戏", ("游戏", "手游", "端游", "版号")),
    ("通信", ("通信", "5G", "光模块", "运营商", "通信设备")),
    ("光通信", ("光模块", "光通信", "CPO", "硅光")),
    ("电力", ("电力", "电网", "发电", "绿电", "火电")),
    ("核电", ("核电", "核能", "核电站")),
    ("氢能", ("氢能", "燃料电池", "电解槽")),
    ("充电桩", ("充电桩", "充电", "换电")),
    ("环保", ("环保", "碳中和", "碳交易", "固废")),
    ("航运物流", ("航运", "港口", "海运", "物流", "快递")),
    ("旅游酒店", ("旅游", "酒店", "免税", "出行", "景区")),
    ("食品饮料", ("食品", "饮料", "乳业", "调味品")),
    ("纺织服装", ("纺织", "服装", "鞋帽")),
    ("钢铁", ("钢铁", "钢材", "特钢")),
    ("化工", ("化工", "化肥", "农药", "塑料", "化纤")),
    ("建材", ("建材", "玻璃", "陶瓷", "水泥")),
    ("机械", ("机械", "工程机械", "机床", "工业母机")),
    ("电子", ("电子", "消费电子", "面板", "PCB", "电子元器件")),
    ("软件", ("软件", "信创", "SaaS", "云计算", "操作系统")),
    ("互联网", ("互联网", "平台经济", "电商", "流量")),
    ("数据要素", ("数据要素", "数据资产", "数据确权", "数据交易")),
    ("教育", ("教育", "培训", "职业教育")),
]

# 规则路径：行业命中后归入利好/利空/关注的说明文案
_RULE_REASON = {
    "利好": "快讯整体偏利好，该行业或受资金关注",
    "利空": "快讯整体偏利空，该行业或承压",
    "中性": "快讯提及该行业，暂无明显多空信号",
}


def _extract_keywords(text: str) -> list[str]:
    """文本命中哪些行业词典关键词，去重返回（按词典顺序，最多 6 个）。"""
    out: list[str] = []
    for _industry, kws in _SECTORS:
        for kw in kws:
            if kw in text and kw not in out:
                out.append(kw)
    return out[:6]


def rule_analyze(
    title: str, summary: str
) -> tuple[str, list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[str]]:
    """规则引擎分析（LLM 不可用/失败时的兜底）。

    返回 (情绪, 利好行业, 利空行业, 关注行业, 检索关键词)。
    行业归属跟随整体情绪：利好→利好行业，利空→利空行业，中性→关注行业。
    """
    text = f"{title} {summary}"
    sentiment = news.rule_interpret({"title": title, "summary": summary})["sentiment"]
    keywords = _extract_keywords(text)

    hit: list[str] = []
    for industry, kws in _SECTORS:
        if any(kw in text for kw in kws) and industry not in hit:
            hit.append(industry)

    reason = _RULE_REASON[sentiment]
    if sentiment == "利好":
        bull = [{"industry": i, "reason": reason} for i in hit]
        return sentiment, bull, [], [], keywords
    if sentiment == "利空":
        bear = [{"industry": i, "reason": reason} for i in hit]
        return sentiment, [], bear, [], keywords
    return sentiment, [], [], [{"industry": i, "reason": reason} for i in hit], keywords


# ------------------------------------------------------------------ LLM 分析

_ANALYZE_SYSTEM = """你是 A 股市场快讯影响分析助手。你会收到一条财经快讯（标题/摘要/来源）。

任务：判断该快讯利好哪些行业、利空哪些行业，并给出可用于检索相关 A 股的关键词。

只输出一个 JSON 对象，不要输出任何其他文字或代码块，结构：
{
  "sentiment": "利好|利空|中性",
  "bullish_industries": [{"industry": "行业或概念名", "reason": "利好逻辑（30字内）"}],
  "bearish_industries": [{"industry": "行业或概念名", "reason": "利空逻辑（30字内）"}],
  "watch_industries": [{"industry": "行业或概念名", "reason": "提及但方向不明（30字内）"}],
  "keywords": ["用于检索相关 A 股的关键词：涉及的公司名、行业名、概念名、产品名，2-6 个"]
}

要求：
1. sentiment 三选一。
2. 每个行业列表最多 4 项，industry 用简洁名称（如：光伏、半导体、白酒、券商、创新药）。
3. keywords 必须能直接命中 A 股股票名称或行业板块，避免过于宽泛（如"市场""国家""利好"）。
4. 快讯与行业影响无关时 bullish/bearish 可为空，sentiment 用"中性"。
"""


async def _llm_analyze(
    title: str, summary: str, source: str
) -> dict[str, Any] | None:
    """LLM 分析。成功返回结构化结果，失败返回 None（由调用方回退规则引擎）。"""
    if not llm.available():
        return None
    user = (
        f"快讯标题：{title}\n"
        f"快讯摘要：{(summary or '').strip()}\n"
        f"来源媒体：{(source or '').strip()}"
    )
    try:
        raw, meta = await llm.chat_json(_ANALYZE_SYSTEM, user)
    except llm.LLMError as exc:
        log.warning("热点快讯 AI 分析失败，回退规则引擎：%s", exc)
        return None

    sentiment = str(raw.get("sentiment") or "中性")
    if sentiment not in ("利好", "利空", "中性"):
        sentiment = "中性"

    def _rows(key: str) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for row in (raw.get(key) or [])[:4]:
            if not isinstance(row, dict):
                continue
            ind = str(row.get("industry") or "").strip()
            if ind:
                out.append({"industry": ind, "reason": str(row.get("reason") or "").strip()})
        return out

    keywords: list[str] = []
    for kw in (raw.get("keywords") or [])[:6]:
        kw = str(kw or "").strip()
        if kw and kw not in keywords:
            keywords.append(kw)
    # 模型没给关键词或关键词过宽时，用行业词典兜底提取
    if not keywords:
        keywords = _extract_keywords(f"{title} {summary}")

    return {
        "sentiment": sentiment,
        "bullish": _rows("bullish_industries"),
        "bearish": _rows("bearish_industries"),
        "watch": _rows("watch_industries"),
        "keywords": keywords,
        "model": meta.get("model", ""),
    }


# ------------------------------------------------------------------ 关联股票解析
# 关键点：股票代码/名称一律来自真实搜索接口，绝不采用模型生成的代码，杜绝编造。

# 检索引擎名 → 展示名（标注每只关联股由哪个数据源检索命中）
_SRC_LABELS = {
    "eastmoney": "东方财富", "ths": "同花顺", "tencent": "腾讯",
    "sina": "新浪财经", "akshare": "AkShare",
}


def _is_stock_code(code: str, market: str) -> bool:
    """筛掉搜索接口混入的 ETF/LOF/基金等非普通 A 股。

    东方财富 suggest 接口会把部分 ETF 也标成「A股」（如 159819 人工智能ETF），
    关联股票必须是能直接交易的个股：沪 60/68、深 00/30、北 43/83/87/88/920。
    """
    if market == "SH":
        return code.startswith(("60", "68"))
    if market == "BJ":
        return code.startswith(("43", "83", "87", "88", "920"))
    return code.startswith(("00", "30"))


async def _search_one(kw: str) -> tuple[list[Any], str]:
    """按关键词检索真实 A 股，返回 (个股列表, 检索来源展示名)。"""
    try:
        items, src = await registry().search_with_source(kw, 6)
    except Exception as exc:  # noqa: BLE001 - 单个关键词失败不影响其余
        log.info("热点关联股检索 %s 失败：%s", kw, exc)
        return [], ""
    filtered = [it for it in items if _is_stock_code(it.code, it.market)]
    return filtered, _SRC_LABELS.get(src, src)


async def _resolve_stocks(keywords: list[str], limit: int = 3) -> list[dict[str, Any]]:
    """按关键词检索真实 A 股，去重，按「命中关键词数 + 首次出现顺序」排序，取前 limit。"""
    if not keywords:
        return []
    results = await asyncio.gather(
        *(_search_one(kw) for kw in keywords), return_exceptions=True
    )
    ranked: dict[str, dict[str, Any]] = {}
    order = 0
    for kw, res in zip(keywords, results):
        if isinstance(res, BaseException):
            continue
        items, src = res
        for item in items:
            key = full_code(item.code, item.market)
            entry = ranked.get(key)
            if entry is None:
                entry = {
                    "code": item.code,
                    "market": item.market,
                    "name": item.name,
                    "keywords": [],
                    "matches": [],
                    "first_pos": order,
                }
                ranked[key] = entry
            entry["keywords"].append(kw)
            # 命中明细：哪个检索词 + 由哪个数据源检索到，供前端展示关联依据
            entry["matches"].append({"keyword": kw, "source": src})
            order += 1
    if not ranked:
        return []
    ordered = sorted(ranked.values(), key=lambda e: (-len(e["keywords"]), e["first_pos"]))
    return ordered[:limit]


async def _with_quotes(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给关联股补当前行情（尽力而为：失败不阻塞分析）。"""
    if not stocks:
        return stocks
    try:
        quotes = await service.get_quotes([(s["code"], s["market"]) for s in stocks])
    except Exception as exc:  # noqa: BLE001
        log.info("热点关联股行情获取失败（忽略）：%s", exc)
        quotes = {}
    for s in stocks:
        q = quotes.get(full_code(s["code"], s["market"]))
        if q:
            s["price"] = q.price
            s["change_pct"] = q.change_pct
            s["board"] = q.board or ""
        # 关联理由：命中的关键词（即股票名/板块名里包含的检索词）
        s["reason"] = "、".join(s["keywords"]) or s.get("board", "") or s["name"]
    return stocks


def _fp(title: str, summary: str) -> str:
    raw = f"{title}\n{summary}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ 组装入口

async def analyze_news(
    title: str, summary: str = "", source: str = "", force: bool = False
) -> dict[str, Any]:
    """分析单条快讯。

    返回 {ok, sentiment, bullish[], bearish[], watch[], stocks[], engine, model, ...}。
    - bullish/bearish/watch：{industry, reason} 列表
    - stocks：{code, name, market, price?, change_pct?, board?, reason} 最多 3 只
    """
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "快讯标题为空"}

    key = f"hotspot_ai:{_fp(title, summary)}"

    async def load() -> dict[str, Any]:
        parsed = await _llm_analyze(title, summary, source)
        if parsed:
            engine, model = "llm", parsed["model"]
            sentiment = parsed["sentiment"]
            bull, bear, watch = parsed["bullish"], parsed["bearish"], parsed["watch"]
            keywords = parsed["keywords"]
        else:
            engine, model = "rule", ""
            sentiment, bull, bear, watch, keywords = rule_analyze(title, summary)
        stocks = await _resolve_stocks(keywords)
        await _with_quotes(stocks)
        return {
            "ok": True,
            "sentiment": sentiment,
            "bullish": bull,
            "bearish": bear,
            "watch": watch,
            "stocks": stocks,
            "engine": engine,
            "model": model,
            "title": title,
            "summary": (summary or "").strip(),
            "source": (source or "").strip(),
            "fetched_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    try:
        result = await cache.get_or_set(key, TTL, load, force=force)
        # 补当前自选状态（在缓存外计算，保证每次打开弹窗都是最新）
        for s in result.get("stocks") or []:
            s["watched"] = storage.is_watched(s["code"])
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("热点快讯分析失败：%s", describe_exc(exc))
        return {"ok": False, "error": f"分析失败：{describe_exc(exc)}"}
