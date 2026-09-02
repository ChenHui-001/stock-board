"""全局配置：全部通过环境变量注入，便于 Docker 部署。"""
from __future__ import annotations

import os
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


def _clamp_llm_timeout(v: float) -> float:
    """LLM 单次调用超时下限：主分析要生成最多 LLM_MAX_TOKENS 个 token 的 JSON，

    DeepSeek 等公有云盘中高峰常需 60s 以上。旧部署（.env / compose）里可能
    残留 45s 等过小配置，会把正常生成掐断并降级规则引擎——低于下限的一律
    提到下限，保证 AI 分析可用（用户仍可调大，但不能调得过小）。
    """
    return max(v, 90.0)


def _clamp_llm_total_timeout(v: float, single: float) -> float:
    """多档案串行故障转移的总预算下限：至少要容得下一个档案跑满 LLM_TIMEOUT。

    总预算小于单档案超时的话，连第一个档案都会被提前掐断，故障转移形同虚设
    （表现为「永远是第一个源超时」，永远换不到备用源）。用户仍可调大，但不能
    调得比单档案超时还小。
    """
    return max(v, single)


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


class Settings:
    # ---------- 基础 ----------
    APP_NAME = "股票看板"
    TZ = os.getenv("TZ", "Asia/Shanghai")
    # Docker 内由 Dockerfile 设为 /app/data；本地开发落到项目下的 ./data
    DATA_DIR = Path(os.getenv("DATA_DIR") or (Path(__file__).resolve().parent.parent / "data"))
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = _int("PORT", 8000)

    # ---------- 数据源 ----------
    # 顺序即故障转移优先级，东方财富为主，其余为辅。
    # 同花顺(ths)排在腾讯/新浪之前：它的 K 线来自同花顺行情网页加载的真实数据文件
    # （d.10jqka.com.cn/v6/line/.../last.js，含最新交易日），东财 K 线不可用时
    # 优先回退到网页数据文件而非腾讯/新浪接口，保证详情页数据真实性。
    PROVIDER_ORDER = _list(
        "PROVIDER_ORDER", ["eastmoney", "ths", "tencent", "sina", "akshare"]
    )
    ENABLE_AKSHARE = _bool("ENABLE_AKSHARE", False)
    HTTP_TIMEOUT = _float("HTTP_TIMEOUT", 6.0)
    HTTP_RETRY = _int("HTTP_RETRY", 2)

    # ---------- 缓存 TTL（秒）----------
    QUOTE_TTL_OPEN = _float("QUOTE_TTL_OPEN", 2.5)      # 盘中实时行情
    QUOTE_TTL_CLOSED = _float("QUOTE_TTL_CLOSED", 60.0)  # 盘后
    HISTORY_TTL_OPEN = _float("HISTORY_TTL_OPEN", 120.0)  # K线/资金/两融 盘中
    HISTORY_TTL_CLOSED = _float("HISTORY_TTL_CLOSED", 900.0)
    # AI 报告当日缓存时效：盘中点击 AI 分析时超过该时长即用最新数据重建
    # （避免命中几小时前的快照；刚分析完短时间内再点仍复用，防止重复打 LLM）
    AI_CACHE_TTL_OPEN = _float("AI_CACHE_TTL_OPEN", 120.0)
    AI_CACHE_TTL_CLOSED = _float("AI_CACHE_TTL_CLOSED", 3600.0)  # 盘后数据不变，放宽
    SEARCH_TTL = _float("SEARCH_TTL", 300.0)
    HOT_TTL = _float("HOT_TTL", 60.0)
    # 市场热点追踪：时间窗（分钟）与聚合结果缓存（秒）
    HOTSPOT_MINUTES = _int("HOTSPOT_MINUTES", 30)
    HOTSPOT_TTL = _float("HOTSPOT_TTL", 90.0)
    # 多源并行抓取的总预算（秒）：超预算的慢源直接丢弃并在 meta.sources 标 timeout，
    # 不拖住整个响应。源数量增加后这是响应时间的硬上限。
    # 默认 8s：实测 6 源全部正常 < 0.5s；保留 8s 余量应对 1-2 源同时慢响应。
    HOTSPOT_BUDGET = _float("HOTSPOT_BUDGET", 8.0)
    # 各源差异化超时（秒）。按实测稳定性分级：
    #   - 4s: 长期稳定的快源（同花顺/华尔街见闻/新浪）；
    #   - 6s: 标准源（东财/财联社），偶发 5xx 抖动；
    #   - 10s: 慢源（金十数据），首次访问常有 1-3s 冷启动。
    # 总和超过 HOTSPOT_BUDGET 时由并行 gather 兜底截断。
    HOTSPOT_TIMEOUT_FAST = _float("HOTSPOT_TIMEOUT_FAST", 4.0)
    HOTSPOT_TIMEOUT_NORMAL = _float("HOTSPOT_TIMEOUT_NORMAL", 6.0)
    HOTSPOT_TIMEOUT_SLOW = _float("HOTSPOT_TIMEOUT_SLOW", 10.0)
    # 单源熔断：连续失败 N 次后熔断冷却 C 秒，期间不再打该源；
    # 冷却到期后下次调用作为半开放尝试，成功则恢复，失败重新打熔断。
    HOTSPOT_CIRCUIT_OPEN_AT = _int("HOTSPOT_CIRCUIT_OPEN_AT", 3)
    HOTSPOT_CIRCUIT_COOLDOWN = _float("HOTSPOT_CIRCUIT_COOLDOWN", 120.0)
    # 榜单快照（涨停池/涨幅榜/龙虎榜/异动/人气榜）缓存：盘中变化快，60s
    HOTSPOT_BOARD_TTL = _float("HOTSPOT_BOARD_TTL", 60.0)
    # 热点搜索结果缓存（秒）：同一关键词短时间内重复搜索不重打上游
    HOTSPOT_SEARCH_TTL = _float("HOTSPOT_SEARCH_TTL", 300.0)

    # ---------- 全网搜索（可选，未配置时热点搜索走站内全文检索）----------
    # 配了 key 才启用全网轨；provider 支持 serper / tavily（都是一次 JSON POST）。
    SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "").strip()
    SEARCH_API_PROVIDER = os.getenv("SEARCH_API_PROVIDER", "serper").strip().lower()

    # ---------- LLM（OpenAI 兼容协议）----------
    LLM_ENABLED = _bool("LLM_ENABLED", True)
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
    LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
    # 单次 LLM 调用超时。主分析要生成最多 LLM_MAX_TOKENS 个 token 的 JSON，
    # DeepSeek 等公有云在 A 股盘中高峰生成 4000 token 常需 60s 以上，
    # 45s 会在模型正常工作时把它掐断（表现为「等待响应超时」并降级规则引擎）。
    # 默认 120s 留足余量，且经 _clamp_llm_timeout 保底 ≥90s：旧 .env/compose
    # 里的 45s 等过小配置无法再生效。
    LLM_TIMEOUT = _clamp_llm_timeout(_float("LLM_TIMEOUT", 120.0))
    # 多档案串行故障转移的「总预算」：chat_json 会依次尝试每个档案，单档案最坏
    # 跑满 LLM_TIMEOUT(120s)，4 个档案串行就是 8 分钟——HTTP 请求早就断开了，
    # 用户只看到转圈。180s = 单档案 120s + 约 60s 换源机会：主源超时后还能让
    # 备用源跑一段，实在不行则由总预算兜底抛 LLMError 走规则引擎降级。
    # 经 _clamp_llm_total_timeout 保底不低于 LLM_TIMEOUT，避免配成比单档案还小。
    LLM_TOTAL_TIMEOUT = _clamp_llm_total_timeout(
        _float("LLM_TOTAL_TIMEOUT", 180.0), LLM_TIMEOUT
    )
    LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 4000)
    # 思考类模型（deepseek-reasoner / *-thinking / r1 等）的思考过程也占用输出配额，
    # 配额不足时正文 content 会为空；检测到思考型模型或空正文时自动放大到此值重试。
    LLM_THINKING_MAX_TOKENS = _int("LLM_THINKING_MAX_TOKENS", 8192)
    LLM_TEMPERATURE = _float("LLM_TEMPERATURE", 0.25)
    # 部分兼容端点不支持 response_format=json_object，可关闭
    LLM_JSON_MODE = _bool("LLM_JSON_MODE", True)

    # ---------- 业务参数 ----------
    KLINE_LIMIT = _int("KLINE_LIMIT", 260)   # 需覆盖 MA60 与 30 日趋势
    FLOW_DAYS = _int("FLOW_DAYS", 30)
    MARGIN_DAYS = _int("MARGIN_DAYS", 30)

    # ---------- AI 评分权重 ----------
    # 三维分面评分（技术面/资金面/消息面）的乘数，默认 1.0，范围 0.2~3.0。
    # 调大某面让该维度信号更强（如消息面重仓者可调高 NEWS）；
    # 设置页保存的权重优先于环境变量。
    SCORE_WEIGHT_TECH = _float("SCORE_WEIGHT_TECH", 1.0)
    SCORE_WEIGHT_CAPITAL = _float("SCORE_WEIGHT_CAPITAL", 1.0)
    SCORE_WEIGHT_NEWS = _float("SCORE_WEIGHT_NEWS", 1.0)

    @property
    def db_path(self) -> Path:
        return self.DATA_DIR / "board.db"


settings = Settings()
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
