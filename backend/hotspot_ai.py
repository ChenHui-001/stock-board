"""热点快讯 AI 分析：单条市场快讯 → 利好/利空行业 + 关联度最高的三只股票。

链路：
1. LLM 可用时：把快讯交给大模型，让它输出整体情绪 + 利好/利空/关注行业 + 检索关键词
   （关键词用于在真实 A 股市场检索关联股票，避免模型编造代码）。
2. LLM 不可用/失败：内置行业词典匹配快讯文本识别行业，情绪用关键词规则判定
   （与个股资讯解读同口径）。
3. 无论哪条路径，关联股票都通过 registry().search() 用真实搜索接口解析，
   保证返回的每只股票代码/名称都是真实存在的；解析后批量取行情快照
   （点击触发 + 600s 缓存），按「命中关键词数 + 涨跌幅/量比信号」重排，
   行情失败自动降级原「命中关键词数 + 检索顺序」排序。

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

# ------------------------------------------------------------------ SECTOR_* 常量区
# 调参只改这里（约定：hotspot_ai.py 内常量一律 SECTOR_ 前缀）。
SECTOR_SENT_WINDOW = 12      # 标签级情绪判定：命中词前后各取多少字
SECTOR_QUOTE_TTL = 600.0     # 相关股行情快照缓存（秒）；点击触发路径，不在 60s 热路径
SECTOR_QUOTE_PCT_W = 0.8     # rank 公式：clamp(pct,-5,10) 的权重
SECTOR_QUOTE_VR_W = 0.6      # rank 公式：clamp(vol_ratio,0,5) 的权重

# 规则路径行业词典（结构化版）：行业名 -> {keywords, parent, generic}。
# 关键词同时用于（a）匹配快讯文本识别行业，（b）检索真实 A 股关联股票。
# - parent：父概念名（如 光伏→新能源）。命中子概念时父概念不重复计数，
#   除非父有独立核心词（非泛词、且不与子概念共享）命中——父子去重用。
# - generic：泛词黑名单（{词: True}）。这些短词区分度低（如"证券""汽车"），
#   仅在标题命中时才计分/保留标签，摘要命中不生效（泛词门槛）。
_SECTOR_DICT: dict[str, dict[str, Any]] = {
    "新能源": {"keywords": ("新能源", "光伏", "风电", "储能", "氢能", "锂电", "电池"), "parent": None, "generic": {"电池": True}},
    "光伏": {"keywords": ("光伏", "硅料", "组件", "逆变器", "HJT", "TOPCon"), "parent": "新能源", "generic": {}},
    "锂电池": {"keywords": ("锂电池", "锂电", "正极", "负极", "隔膜", "电解液", "碳酸锂"), "parent": "新能源", "generic": {}},
    "储能": {"keywords": ("储能", "电化学储能", "抽水蓄能"), "parent": "新能源", "generic": {}},
    "半导体/芯片": {"keywords": ("半导体", "芯片", "集成电路", "晶圆", "光刻", "封测"), "parent": None, "generic": {"半导体": True, "芯片": True}},
    "半导体设备": {"keywords": ("半导体设备", "光刻机", "刻蚀"), "parent": "半导体/芯片", "generic": {}},
    "人工智能": {"keywords": ("人工智能", "AI大模型", "大模型", "算力", "AIGC", "机器人"), "parent": None, "generic": {"算力": True, "机器人": True}},
    "算力": {"keywords": ("算力", "数据中心", "服务器", "液冷"), "parent": "人工智能", "generic": {}},
    "机器人": {"keywords": ("机器人", "人形机器人", "减速器", "伺服"), "parent": "人工智能", "generic": {}},
    "医药": {"keywords": ("医药", "创新药", "疫苗", "CXO", "医疗器械", "生物医药"), "parent": None, "generic": {"医药": True}},
    "创新药": {"keywords": ("创新药", "GLP-1", "ADC", "双抗"), "parent": "医药", "generic": {}},
    "医疗器械": {"keywords": ("医疗器械", "医疗设备", "耗材"), "parent": "医药", "generic": {"耗材": True}},
    "白酒": {"keywords": ("白酒", "茅台", "五粮液", "酿酒", "啤酒"), "parent": None, "generic": {"酿酒": True, "啤酒": True}},
    "地产": {"keywords": ("房地产", "地产", "楼市", "房价", "土拍"), "parent": None, "generic": {"地产": True, "楼市": True, "房价": True}},
    "银行": {"keywords": ("银行", "信贷", "降息", "LPR", "存贷款"), "parent": None, "generic": {"信贷": True, "降息": True}},
    "券商": {"keywords": ("券商", "证券", "投行", "资本市场", "经纪"), "parent": None, "generic": {"证券": True, "投行": True, "经纪": True}},
    "保险": {"keywords": ("保险", "寿险", "财险", "保费"), "parent": None, "generic": {"保险": True, "保费": True}},
    "军工": {"keywords": ("军工", "国防", "航天", "航空", "导弹"), "parent": None, "generic": {"航天": True, "航空": True}},
    "卫星互联网": {"keywords": ("卫星互联网", "卫星", "北斗", "商业航天"), "parent": None, "generic": {"卫星": True}},
    "汽车": {"keywords": ("汽车", "新能源车", "整车", "智能驾驶", "汽车零部件"), "parent": None, "generic": {"汽车": True}},
    "低空经济": {"keywords": ("低空经济", "eVTOL", "无人机", "飞行汽车"), "parent": None, "generic": {"无人机": True}},
    "消费": {"keywords": ("消费", "零售", "电商", "免税", "家电"), "parent": None, "generic": {"消费": True, "零售": True, "电商": True}},
    "家电": {"keywords": ("家电", "空调", "白电", "小家电"), "parent": "消费", "generic": {"家电": True}},
    "黄金": {"keywords": ("黄金", "金价", "贵金属"), "parent": None, "generic": {"金价": True}},
    "煤炭": {"keywords": ("煤炭", "煤价", "焦煤", "动力煤"), "parent": None, "generic": {"煤价": True}},
    "石油石化": {"keywords": ("石油", "原油", "油气", "油价", "炼化"), "parent": None, "generic": {"石油": True, "油价": True}},
    "有色金属": {"keywords": ("有色", "铜", "铝", "稀土", "锂矿", "镍"), "parent": None, "generic": {"有色": True, "铜": True, "铝": True, "镍": True}},
    "农业": {"keywords": ("农业", "粮食", "种业", "猪肉", "养殖", "饲料"), "parent": None, "generic": {"养殖": True, "饲料": True, "粮食": True}},
    "基建": {"keywords": ("基建", "工程", "建筑", "水泥", "装配式"), "parent": None, "generic": {"工程": True, "建筑": True}},
    "传媒": {"keywords": ("传媒", "影视", "游戏", "广告", "出版"), "parent": None, "generic": {"广告": True, "出版": True}},
    "游戏": {"keywords": ("游戏", "手游", "端游", "版号"), "parent": "传媒", "generic": {}},
    "通信": {"keywords": ("通信", "5G", "光模块", "运营商", "通信设备"), "parent": None, "generic": {"通信": True, "5G": True}},
    "光通信": {"keywords": ("光模块", "光通信", "CPO", "硅光"), "parent": "通信", "generic": {}},
    "电力": {"keywords": ("电力", "电网", "发电", "绿电", "火电"), "parent": None, "generic": {"发电": True, "电网": True}},
    "核电": {"keywords": ("核电", "核能", "核电站"), "parent": "电力", "generic": {}},
    "氢能": {"keywords": ("氢能", "燃料电池", "电解槽"), "parent": "新能源", "generic": {}},
    "充电桩": {"keywords": ("充电桩", "充电", "换电"), "parent": "新能源", "generic": {"充电": True}},
    "环保": {"keywords": ("环保", "碳中和", "碳交易", "固废"), "parent": None, "generic": {"环保": True}},
    "航运物流": {"keywords": ("航运", "港口", "海运", "物流", "快递"), "parent": None, "generic": {"物流": True, "港口": True}},
    "旅游酒店": {"keywords": ("旅游", "酒店", "免税", "出行", "景区"), "parent": "消费", "generic": {"旅游": True, "酒店": True, "出行": True}},
    "食品饮料": {"keywords": ("食品", "饮料", "乳业", "调味品"), "parent": "消费", "generic": {"食品": True, "饮料": True}},
    "纺织服装": {"keywords": ("纺织", "服装", "鞋帽"), "parent": None, "generic": {"纺织": True, "服装": True}},
    "钢铁": {"keywords": ("钢铁", "钢材", "特钢"), "parent": None, "generic": {"钢铁": True, "钢材": True}},
    "化工": {"keywords": ("化工", "化肥", "农药", "塑料", "化纤"), "parent": None, "generic": {"化工": True, "化肥": True, "农药": True, "塑料": True, "化纤": True}},
    "建材": {"keywords": ("建材", "玻璃", "陶瓷", "水泥"), "parent": None, "generic": {"玻璃": True, "陶瓷": True, "水泥": True}},
    "机械": {"keywords": ("机械", "工程机械", "机床", "工业母机"), "parent": None, "generic": {"机械": True}},
    "电子": {"keywords": ("电子", "消费电子", "面板", "PCB", "电子元器件"), "parent": None, "generic": {"电子": True}},
    "软件": {"keywords": ("软件", "信创", "SaaS", "云计算", "操作系统"), "parent": None, "generic": {"软件": True, "云计算": True}},
    "互联网": {"keywords": ("互联网", "平台经济", "电商", "流量"), "parent": None, "generic": {"互联网": True, "流量": True}},
    "数据要素": {"keywords": ("数据要素", "数据资产", "数据确权", "数据交易"), "parent": None, "generic": {}},
    "教育": {"keywords": ("教育", "培训", "职业教育"), "parent": None, "generic": {"教育": True, "培训": True}},
}

# 兼容适配：老代码（_extract_keywords / rule_analyze / 测试）仍按 (行业名, 关键词) 列表消费。
# 词典顺序与旧 _SECTORS 完全一致，行为不漂移。
_SECTORS: list[tuple[str, tuple[str, ...]]] = [
    (name, tuple(cfg["keywords"])) for name, cfg in _SECTOR_DICT.items()
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


# ------------------------------------------------------------------ 标签级情绪（规则邻近词表）
# 复用个股资讯解读同一套利好/利空词表，保证口径一致；未来可整函数替换为 LLM 批量判定。

def _tag_sentiment(title: str, summary: str, kw: str, ctx: dict[str, Any] | None = None) -> str:
    """判定单个概念标签的情绪（利好/利空/中性）。

    规则：取命中词 kw 在文本中的位置，前后各 ``window`` 字（默认 SECTOR_SENT_WINDOW）
    组成邻近窗口；窗口内利好词 +1 / 利空词 -1，>0 → 利好、<0 → 利空、否则中性。
    标题命中取标题窗口，摘要命中取摘要窗口。

    ctx：可选覆盖项 {"window": int, "bull_words": list, "bear_words": list}，
    便于单测注入固定词表；也是未来 LLM 实现的扩展点入参（签名稳定，整函数可替换）。
    """
    ctx = ctx or {}
    window = int(ctx.get("window", SECTOR_SENT_WINDOW))
    bull_words = ctx.get("bull_words") or news._BULL_WORDS  # noqa: SLF001 - 同包内复用词表
    bear_words = ctx.get("bear_words") or news._BEAR_WORDS

    def _window_text(text: str) -> str:
        """取 kw 命中位置前后 window 字的邻近文本；未命中返回空串。"""
        pos = text.find(kw)
        if pos < 0:
            return ""
        lo = max(0, pos - window)
        hi = min(len(text), pos + len(kw) + window)
        return text[lo:hi]

    near = _window_text(title or "") or _window_text(summary or "")
    if not near:
        return "中性"
    score = sum(1 for w in bull_words if w in near) - sum(1 for w in bear_words if w in near)
    if score > 0:
        return "利好"
    if score < 0:
        return "利空"
    return "中性"


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


def _quote_rank(entry: dict[str, Any]) -> float:
    """相关股排序分：命中关键词数 ×2 + 涨跌幅信号 + 量比信号。

    rank = kw_hits×2 + clamp(pct, -5, 10)×0.8 + clamp(vol_ratio, 0, 5)×0.6
    行情字段缺失（None）时对应项计 0，不惩罚——退化为纯关键词热度。
    """
    kw_part = len(entry.get("keywords") or []) * 2.0
    pct = entry.get("change_pct")
    pct_part = SECTOR_QUOTE_PCT_W * min(max(float(pct), -5.0), 10.0) if isinstance(pct, (int, float)) else 0.0
    vr = entry.get("volume_ratio")
    vr_part = SECTOR_QUOTE_VR_W * min(max(float(vr), 0.0), 5.0) if isinstance(vr, (int, float)) else 0.0
    return kw_part + pct_part + vr_part


async def _quotes_snapshot(pairs: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """批量取相关股行情快照（点击触发路径，600s 缓存，不在 60s 热路径）。

    返回 {full_code: {price, change_pct, volume_ratio}}（缺字段为 None）；
    失败静默返回 {}，由调用方降级为「命中数 + 检索序」原排序。
    """
    if not pairs:
        return {}
    fp = hashlib.md5(",".join(f"{c}.{m}" for c, m in pairs).encode("utf-8")).hexdigest()[:12]
    key = f"hotspot_ai:quotes:{fp}"

    async def load() -> dict[str, dict[str, Any]]:
        quotes = await service.get_quotes(list(pairs))
        return {
            fc: {
                "price": q.price,
                "change_pct": q.change_pct,
                "volume_ratio": getattr(q, "volume_ratio", None),
            }
            for fc, q in quotes.items()
        }

    try:
        return await cache.get_or_set(key, SECTOR_QUOTE_TTL, load)
    except Exception as exc:  # noqa: BLE001 - 行情失败不阻塞关联股返回
        log.info("热点相关股行情快照获取失败（降级原排序）：%s", describe_exc(exc))
        return {}


async def _resolve_stocks(keywords: list[str], limit: int = 3) -> list[dict[str, Any]]:
    """按关键词检索真实 A 股，去重，再按行情信号重排，取前 limit。

    排序：有行情快照时按 _quote_rank 降序（平局按首次检索序）；行情获取失败
    或全部缺行情时降级原「命中关键词数 + 首次出现顺序」排序。
    """
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
    # 行情信号重排：解析出真实代码后批量取行情快照；失败降级原排序
    quotes = await _quotes_snapshot([(e["code"], e["market"]) for e in ordered])
    if quotes:
        for e in ordered:
            q = quotes.get(full_code(e["code"], e["market"]))
            if q:
                if q.get("price") is not None:
                    e["price"] = q["price"]
                if q.get("change_pct") is not None:
                    e["change_pct"] = q["change_pct"]
                if q.get("volume_ratio") is not None:
                    e["volume_ratio"] = q["volume_ratio"]
            e["rank_score"] = round(_quote_rank(e), 3)
        ordered.sort(key=lambda e: (-e.get("rank_score", 0.0), e["first_pos"]))
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
        # 补当前自选状态（在缓存外计算，保证每次打开弹窗都是最新）。
        # 一次取回全部自选代码做集合判定，避免逐行 is_watched 退化成 N+1 次查询。
        watched = await storage.a_watched_codes()
        for s in result.get("stocks") or []:
            s["watched"] = s["code"] in watched
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("热点快讯分析失败：%s", describe_exc(exc))
        return {"ok": False, "error": f"分析失败：{describe_exc(exc)}"}
