"""API 响应模型（P2 #18 全量 response_model 补齐）。

设计要点：
- 全部模型 `extra="allow"`：未在模型里声明的字段**原样保留**在响应里，
  不会因为补 schema 而把前端正在消费的字段过滤掉（序列化契约零破坏）。
- 模型声明的字段提供 OpenAPI 文档与信封级校验；深层的自由结构
  （AI 分析正文、行情快照、诊断明细等）按 `dict[str, Any]` / `list` 处理。
- /api/backtest/run/{id}/report 返回 HTML 文件流，不属于 JSON 信封，
  不设 response_model（response_class=HTMLResponse）。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _AllowExtra(BaseModel):
    """所有响应模型的基类：未知字段原样透传，不做过滤。"""

    model_config = ConfigDict(extra="allow")


# ------------------------------------------------------------------ 元信息 / 健康自检

class HealthCheckResp(_AllowExtra):
    """GET /api/health/check —— 数据源健康自检结果（结构随诊断项动态变化）。"""

    ok: bool | None = None
    error: str | None = None
    count: int | None = None


class MetaResp(_AllowExtra):
    """GET /api/meta —— 应用元信息与数据源/AI 引擎状态。"""

    app: str
    session: dict[str, Any]
    providers: list[dict[str, Any]]
    # 实际类型 dict[str, float]（host -> 剩余限流秒数），见 providers/health.py throttled_hosts()
    throttled_hosts: dict[str, float]
    host_stats: dict[str, dict[str, Any]]
    ai: dict[str, Any]
    refresh: dict[str, Any]


# ------------------------------------------------------------------ LLM 配置

class LLMProfileInfo(_AllowExtra):
    """/llm/config 返回的单份档案（api_key 永不回显，只给 api_key_set）。"""

    id: str = ""
    name: str = ""
    enabled: bool = True
    primary: bool = False
    vendor: str = "custom"
    base_url: str = ""
    model: str = ""
    api_key_set: bool = False
    json_mode: bool = True


class LLMConfigResp(_AllowExtra):
    """GET /api/llm/config"""

    profiles: list[LLMProfileInfo]
    engine: str
    vendors: list[dict[str, Any]]


class LLMConfigSaveResp(_AllowExtra):
    """POST /api/llm/config"""

    ok: bool
    profiles: list[LLMProfileInfo]
    engine: str


class LLMTestResp(_AllowExtra):
    """POST /api/llm/test —— 单份档案最小连通性测试。"""

    ok: bool
    message: str


class LLMModelsResp(_AllowExtra):
    """POST /api/llm/models —— 拉取云端模型列表。"""

    ok: bool
    models: list[Any]
    message: str


class OkResp(_AllowExtra):
    """通用 ok 信封。"""

    ok: bool


# ------------------------------------------------------------------ AI 评分 / 价值选股权重

class ScoreWeightsResp(_AllowExtra):
    """GET/POST /api/score/weights、POST reset —— 三维分面权重（动态维度键 + 元字段）。"""

    range: list[float]
    source: str


class ValueWeightsResp(_AllowExtra):
    """GET/POST /api/value/weights、POST reset —— 价值选股维度权重。"""

    range: list[float]
    maxes: dict[str, Any]
    base_total: float
    source: str


# ------------------------------------------------------------------ 自选股

class WatchlistResp(_AllowExtra):
    """GET /api/watchlist —— 自选股看板（每行含实时行情与监测字段）。"""

    items: list[dict[str, Any]]
    session: dict[str, Any]


class AddWatchResp(_AllowExtra):
    """POST /api/watchlist"""

    ok: bool
    created: Any
    code: str
    name: str | None
    board: str | None


class RemoveWatchResp(_AllowExtra):
    """POST /api/watchlist/remove"""

    ok: bool
    removed: int


# ------------------------------------------------------------------ 查询 / 热点

class SearchResp(_AllowExtra):
    """GET /api/search —— 股票搜索。"""

    keyword: str
    items: list[dict[str, Any]]


class HotResp(_AllowExtra):
    """GET /api/hot —— 涨幅/跌幅/活跃榜（附自选标记）。

    实际返回为顶层 gainers/losers/actives 三个列表（service.hot 展开缓存包），
    缓存 loader 里的 {"data": ...} 包裹不会出现在最终响应中。
    """

    gainers: list[dict[str, Any]] = []
    losers: list[dict[str, Any]] = []
    actives: list[dict[str, Any]] = []
    stale: bool = False
    error: str = ""


class HotspotResp(_AllowExtra):
    """GET /api/hotspot 与 GET /api/hotspot/search —— 快讯列表（同一 shape）。"""

    items: list[dict[str, Any]]
    meta: dict[str, Any]


class HotspotAnalyzeResp(_AllowExtra):
    """POST /api/hotspot/analyze —— 快讯 AI 分析（结构随引擎动态变化）。"""


class ValueScreenResp(_AllowExtra):
    """GET /api/value/screen —— 价值选股聚合结果（市场环境/板块强度/分级池等）。"""


# ------------------------------------------------------------------ 个股

class StockDetailResp(_AllowExtra):
    """GET /api/stock/{code} —— 详情页聚合 payload（analysis.build_payload）。"""

    quote: dict[str, Any]
    # 实际为纯名字列表（_board_names() 转换，向后兼容老前端），结构化板块走 boards_detail
    boards: list[str]
    kline: list[dict[str, Any]]
    ma: list[dict[str, Any]]
    ma_summary: dict[str, Any]
    support_resistance: dict[str, Any]
    fund_flow: dict[str, Any]
    margin: dict[str, Any]
    status: dict[str, Any]


class QuoteResp(_AllowExtra):
    """GET /api/quote/{code} —— 轻量行情（详情页自动刷新）。"""

    quote: dict[str, Any]
    session: dict[str, Any]


class StockNewsResp(_AllowExtra):
    """GET /api/news/{code} —— 个股资讯（逐条附 AI 解读）。"""

    items: list[dict[str, Any]]


class StockReportsResp(_AllowExtra):
    """GET /api/reports/{code} —— 券商研报列表与评级分布。"""

    items: list[dict[str, Any]]
    rating_dist: dict[str, Any]


# ------------------------------------------------------------------ AI 分析

class AIWatchlistResp(_AllowExtra):
    """GET/POST /api/ai/watchlist —— 自选股批量 AI 轻量摘要。"""

    items: list[dict[str, Any]]
    total: int
    analyzed: int
    refresh: bool


class AIReportResp(_AllowExtra):
    """POST /api/ai/{code} —— 单股完整 AI 分析报告。"""

    code: str
    name: str | None
    board: str | None
    price: float | None
    change_pct: float | None
    analysis: dict[str, Any]
    meta: dict[str, Any]
    status_tags: list[Any]
    report_sentiment: dict[str, Any]
    rating_dist: dict[str, Any]
    reports_preview: list[dict[str, Any]]
    from_cache: bool


# ------------------------------------------------------------------ 回测（/api/backtest/*）

class StrategiesResp(_AllowExtra):
    """GET /api/backtest/strategies —— 策略清单（含参数 schema）。"""

    strategies: list[dict[str, Any]]
    running: str | None


class BacktestMetaResp(_AllowExtra):
    """POST /api/backtest/run 与 GET /api/backtest/run/{id} —— 运行元信息/状态/结果。"""

    run_id: str | None = None
    status: str | None = None


class RunsListResp(_AllowExtra):
    """GET /api/backtest/runs —— 历史运行列表。"""

    runs: list[dict[str, Any]]
    running: str | None


class RunDeleteResp(_AllowExtra):
    """DELETE /api/backtest/run/{id}"""

    ok: bool
    run_id: str


class RunTradesResp(_AllowExtra):
    """GET /api/backtest/run/{id}/trades —— 事件明细抽样下发。"""

    rows: list[dict[str, Any]]
    limit: int
