"""HTTP API 路由。"""
from __future__ import annotations
import re

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import analysis, hotspot as hotspot_mod, hotspot_ai, hotspot_search, llm, llmcfg, news as news_mod, reports as reports_mod, scorecfg, service, storage, value_screener, valuecfg
from .api_deps import (
REPORT_SCHEMA_VERSION,
_BLANK_LLM_REASON_RE,  # noqa: F401
_ai_locks,  # noqa: F401  re-exported for tests `from backend import api; api._ai_locks`
_ai_summary_from_report,  # noqa: F401
_cache_fresh,
_cached_brief_report,  # noqa: F401
_cached_report,
_fail,
_mark_value_watched,  # noqa: F401
_sentiment_stats,
_with_ai_lock,
)
from .config import settings
from .providers import ProviderError, registry
from .utils import describe_exc, is_trading_now, normalize_code, resolve_market

log = logging.getLogger("api")
router = APIRouter(prefix="/api")

# 回测独立子模块：路由在 api_backtest.py，挂到主 router 上（prefix 已含 /api）
from .api_backtest import router as backtest_router  # noqa: E402
router.include_router(backtest_router)


class AddWatchBody(BaseModel):
    code: str = Field(..., description="6 位股票代码")
    name: str | None = None
    board: str | None = None


class CodesBody(BaseModel):
    codes: list[str] = Field(default_factory=list)


class LLMProfileBody(BaseModel):
    """单份模型档案（多模型配置中的一份）。"""
    id: str = ""
    name: str = ""
    enabled: bool = True
    primary: bool = False
    vendor: str = "custom"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    json_mode: bool = True
    clear_key: bool = False


class LLMConfigBody(BaseModel):
    profiles: list[LLMProfileBody] = Field(default_factory=list)


class HotspotAnalyzeBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=500, description="快讯标题")
    summary: str = Field("", max_length=2000, description="快讯摘要")
    source: str = Field("", max_length=100, description="来源媒体")


# ------------------------------------------------------------------ 数据源健康自检
# 逐源实测各能力（会真实请求数据源，较慢），同一时刻只允许一次探测，避免刷新风暴。
_health_lock = asyncio.Lock()


@router.get("/health/check")
async def health_check(with_backtest: int = 1, backtest_days: int = 120) -> dict[str, Any]:
    """数据源健康自检。with_backtest=0 时跳过盘口回测段（更快）；
    backtest_days 控制回测样本深度（30-250 交易日）。"""
    from . import check_sources

    if not (30 <= backtest_days <= 250):
        raise HTTPException(status_code=400, detail="backtest_days 应在 30-250 之间")
    if _health_lock.locked():
        raise HTTPException(status_code=409, detail="已有自检正在进行，请稍候")
    async with _health_lock:
        try:
            return await asyncio.wait_for(
                check_sources.run_diagnostics(None, with_backtest=bool(with_backtest),
                                              backtest_days=backtest_days),
                timeout=90,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="自检超时（>90s），请稍后重试")
        except Exception as exc:  # noqa: BLE001
            # 防御兜底：数据源限流/探测竞态等偶发异常时返回结构化错误，
            # 不让 FastAPI 默认 500（Internal Server Error）直接透传前端
            raise HTTPException(
                status_code=502,
                detail=f"自检内部错误（{type(exc).__name__}），请稍后重试；"
                       f"若持续出现请查看服务日志",
            )


# ------------------------------------------------------------------ 元信息

@router.get("/meta")
async def meta() -> dict[str, Any]:
    cfg = llmcfg.get_config()
    return {
        "app": settings.APP_NAME,
        "session": service.session_info(),
        "providers": registry().health(),
        "throttled_hosts": registry().throttled_hosts(),
        "host_stats": registry().host_stats(),
        "ai": {
            "enabled": llm.available(),
            "engine": "llm" if llm.available() else "rule",
            "model": cfg["model"] if llm.available() else "内置规则引擎",
            "base_url": cfg["base_url"] if llm.available() else "",
        },
        "refresh": {
            "quote_ttl": service.quote_ttl(),
            "history_ttl": service.history_ttl(),
        },
    }


# ------------------------------------------------------------------ LLM 配置

@router.get("/llm/config")
async def llm_config_get() -> dict[str, Any]:
    profiles = llmcfg.get_profiles()
    return {
        "profiles": [
            {
                "id": p.get("id", ""),
                "name": p.get("name", ""),
                "enabled": p["enabled"],
                "primary": p["primary"],
                "vendor": p["vendor"],
                "base_url": p["base_url"],
                "model": p["model"],
                "api_key_set": bool(p.get("api_key")),
                "json_mode": p["json_mode"],
            }
            for p in profiles
        ],
        "engine": "llm" if llm.available() else "rule",
        "vendors": [
            {"id": key, "label": value["name"], **{
                k: value[k] for k in ("base_url", "model", "json_mode")
            }}
            for key, value in llmcfg.VENDORS.items()
        ],
    }


@router.post("/llm/config")
async def llm_config_save(body: LLMConfigBody) -> dict[str, Any]:
    profiles = [p.model_dump() for p in body.profiles]
    clean = llmcfg.save_profiles(profiles)
    return {
        "ok": True,
        "profiles": [
            {
                "id": p.get("id", ""),
                "name": p.get("name", ""),
                "enabled": p["enabled"],
                "primary": p["primary"],
                "vendor": p["vendor"],
                "base_url": p["base_url"],
                "model": p["model"],
                "api_key_set": bool(p.get("api_key")),
                "json_mode": p["json_mode"],
            }
            for p in clean
        ],
        "engine": "llm" if llm.available() else "rule",
    }


@router.post("/llm/test")
async def llm_config_test(body: LLMProfileBody) -> dict[str, Any]:
    """用某份档案（界面未保存的表单）做一次最小调用。"""
    cfg = llmcfg.merge_pending(body.model_dump(exclude_unset=True))
    ok, message = await llm.test_connection(cfg)
    return {"ok": ok, "message": message}


# ------------------------------------------------------------------ AI 评分权重

@router.get("/score/weights")
async def score_weights_get() -> dict[str, Any]:
    """当前生效的三维分面权重（DB 覆盖优先，环境变量兜底）。"""
    w = scorecfg.get_weights()
    return {
        **w,
        "range": [scorecfg._MIN, scorecfg._MAX],
        "source": "db" if await storage.a_get_kv("score_weights") else "env",
    }


@router.post("/score/weights")
async def score_weights_save(body: dict[str, Any]) -> dict[str, Any]:
    """保存权重（自动 clamp 到合法范围），保存后 AI 当日缓存作废。"""
    w = scorecfg.save_weights(body)
    return {"ok": True, **w}


@router.post("/score/weights/reset")
async def score_weights_reset() -> dict[str, Any]:
    """清除界面配置，回退到环境变量权重。"""
    w = scorecfg.reset_weights()
    return {"ok": True, **w}


@router.post("/llm/models")
async def llm_config_models(body: LLMProfileBody) -> dict[str, Any]:
    """用某份档案从云端拉取模型列表（OpenAI 兼容 GET /models）。"""
    cfg = llmcfg.merge_pending(body.model_dump(exclude_unset=True))
    ok, models, message = await llm.list_models(cfg)
    return {"ok": ok, "models": models, "message": message}


@router.post("/llm/reset")
async def llm_config_reset() -> dict[str, Any]:
    """清除界面保存的配置，回退到环境变量。"""
    llmcfg.reset_config()
    return {"ok": True}


# ------------------------------------------------------------------ 自选股

@router.get("/watchlist")
async def get_watchlist(refresh: bool = Query(False)) -> dict[str, Any]:
    return await service.watchlist_board(force=refresh)


@router.post("/watchlist")
async def add_watchlist(body: AddWatchBody) -> dict[str, Any]:
    code = normalize_code(body.code)
    if not code or len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=400, detail="请输入正确的 6 位 A 股代码")

    name, board = body.name, body.board
    if not name:
        try:
            quote = await service.get_quote(code)
            name, board = quote.name, quote.board
        except ProviderError as exc:
            raise _fail(exc, "无法校验该股票代码") from exc

    # 行情源（腾讯/新浪）不带行业字段时补一次板块查询，入库即有板块，看板不再显示 --
    if not board:
        try:
            names = await service.boards(code)
            board = names[0] if names else None
        except Exception:  # noqa: BLE001 - 板块缺失不影响添加
            board = None

    created = await storage.a_add_watch(code, name, board)
    return {"ok": True, "created": created, "code": code, "name": name, "board": board}


@router.post("/watchlist/remove")
async def remove_watchlist(body: CodesBody) -> dict[str, Any]:
    """批量删除（需求 7.4）。"""
    removed = await storage.a_remove_watch(body.codes)
    return {"ok": True, "removed": removed}


@router.post("/watchlist/order")
async def order_watchlist(body: CodesBody) -> dict[str, Any]:
    """拖拽排序落库（需求 3.2.2）。"""
    await storage.a_reorder_watch(body.codes)
    return {"ok": True}


# ------------------------------------------------------------------ 查询

@router.get("/search")
async def search(
    # 关键词进入缓存 key（service.search），限长以收敛 key 基数；
    # 股票名称/6 位代码/拼音首字母都远短于此，前端输入框同样限制在 32 字符。
    q: str = Query("", min_length=0, max_length=32),
    limit: int = Query(15, ge=1, le=30),
) -> dict[str, Any]:
    items = await service.search(q, limit)
    return {"keyword": q, "items": items}


@router.get("/hot")
async def hot(limit: int = Query(8, ge=1, le=20)) -> dict[str, Any]:
    return await service.hot(limit)


# ------------------------------------------------------------------ 市场热点追踪

@router.get("/hotspot")
async def hotspot(
    minutes: int = Query(30, ge=5, le=120, description="时间窗（分钟）：默认 30"),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    """近 N 分钟市场热点（多源 7x24 快讯聚合：同花顺/东方财富/新浪财经）。

    保留原始媒体署名（彭博社/财联社/财新/澎湃等），命中重点媒体名单的条目
    带 media_badge 标记；结果整体缓存 HOTSPOT_TTL 秒避免反复打外部快讯接口。
    """
    return await hotspot_mod.get_hotspot(minutes=minutes, force=refresh)


@router.get("/hotspot/search")
async def hotspot_search_api(
    # 关键词进入缓存 key，限长以收敛 key 基数（与 /api/search 同口径，前端同样限 32 字符）
    q: str = Query("", min_length=0, max_length=32),
    days: int = Query(7, ge=1, le=30, description="回溯天数：默认 7"),
    limit: int = Query(30, ge=1, le=50),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    """按关键词检索热点资讯——真正打到服务端，不是过滤当前页。

    默认走东方财富站内全文检索（免费）；配置 SEARCH_API_KEY 后改走全网搜索
    （Serper/Tavily），全网轨故障时自动回退站内轨。返回条目 shape 与 /api/hotspot
    一致，前端可直接复用列表渲染。
    """
    return await hotspot_search.search(q, days=days, limit=limit, force=refresh)


@router.post("/hotspot/analyze")
async def hotspot_analyze(
    body: HotspotAnalyzeBody, refresh: bool = Query(False)
) -> dict[str, Any]:
    """单条快讯 AI 分析：利好/利空哪些行业 + 关联度最高的三只股票。

    LLM 可用时由大模型判断行业影响与检索关键词，否则回退内置行业词典；
    关联股票一律通过真实搜索接口解析（代码/名称保证真实），按命中关键词数排序。
    结果按标题+摘要指纹缓存 10 分钟，并发点击同一快讯只执行一次分析。
    """
    return await hotspot_ai.analyze_news(body.title, body.summary, body.source, force=refresh)


# ------------------------------------------------------------------ 价值投资选股

@router.get("/value/screen")
async def value_screen(refresh: bool = Query(False)) -> dict[str, Any]:
    """A 股快速轮动量化选股：市场环境 + 板块强度 + 多维度评分 + 分级池。

    结果聚合缓存 15 分钟；refresh=1 强制重算（逐股拉财务/资金/K线，较慢）。
    """
    try:
        result = await value_screener.run_screen(force=refresh)
        # 补当前自选状态（在缓存外计算，保证每次查看都是最新）
        _mark_value_watched(result, await storage.a_watched_codes())
        return result
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc, "价值选股运行失败") from exc


@router.get("/value/weights")
async def value_weights_get() -> dict[str, Any]:
    """当前生效的价值选股各维度权重（DB 覆盖优先，默认 1.0）。"""
    w = valuecfg.get_weights()
    return {**w, "range": [valuecfg._MIN, valuecfg._MAX],
            "maxes": valuecfg.DIM_MAXES, "base_total": valuecfg.BASE_TOTAL,
            "source": "db" if await storage.a_get_kv("value_weights") else "default"}


@router.post("/value/weights")
async def value_weights_save(body: dict[str, Any]) -> dict[str, Any]:
    """保存权重（自动 clamp 到合法范围），权重变化后选股缓存作废。"""
    w = valuecfg.save_weights(body)
    return {"ok": True, **w}


@router.post("/value/weights/reset")
async def value_weights_reset() -> dict[str, Any]:
    """清除界面权重配置，回退默认 1.0。"""
    w = valuecfg.reset_weights()
    return {"ok": True, **w}


@router.get("/stock/{code}")
async def stock_detail(code: str, refresh: bool = Query(False)) -> dict[str, Any]:
    code = normalize_code(code)
    # 防御性：路径上 {code} 可能被路由到任意字符串（旧的注册顺序 bug，
    # /ai/{code} 抢在 /ai/watchlist 前面），保证只接受 6 位数字股票代码，
    # 否则直接 404，避免把 "watchlist" 这种字面量当股票去请求所有行情源，
    # 触发「所有行情数据源均不可用」误报。
    if not code or not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=404, detail=f"非法的股票代码：{code}")
    try:
        return await service.stock_detail(code, resolve_market(code), force=refresh)
    except ProviderError as exc:
        raise _fail(exc, "获取股票详情失败") from exc


@router.get("/quote/{code}")
async def stock_quote(code: str, refresh: bool = Query(False)) -> dict[str, Any]:
    """轻量行情（详情页自动刷新用）：只返回单只报价，不携带 K线/资金/两融历史。"""
    code = normalize_code(code)
    # 防御性：路径上 {code} 可能被路由到任意字符串（旧的注册顺序 bug，
    # /ai/{code} 抢在 /ai/watchlist 前面），保证只接受 6 位数字股票代码，
    # 否则直接 404，避免把 "watchlist" 这种字面量当股票去请求所有行情源，
    # 触发「所有行情数据源均不可用」误报。
    if not code or not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=404, detail=f"非法的股票代码：{code}")
    try:
        quote = await service.get_quote(code, resolve_market(code), force=refresh)
    except ProviderError as exc:
        raise _fail(exc, "获取行情失败") from exc
    return {"quote": quote.to_dict(), "session": service.session_info()}


# ------------------------------------------------------------------ 个股资讯

@router.get("/news/{code}")
async def stock_news(
    code: str,
    days: int = Query(30, description="时间范围（天）：7/30"),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    """个股资讯（近一个月），逐条附 AI 解读（LLM 不可用/失败时规则引擎兜底）。

    days 控制时间范围（近7天=7 / 近30天=30），资讯列表随范围联动。
    """
    code = normalize_code(code)
    # 防御性：路径上 {code} 可能被路由到任意字符串（旧的注册顺序 bug，
    # /ai/{code} 抢在 /ai/watchlist 前面），保证只接受 6 位数字股票代码，
    # 否则直接 404，避免把 "watchlist" 这种字面量当股票去请求所有行情源，
    # 触发「所有行情数据源均不可用」误报。
    if not code or not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=404, detail=f"非法的股票代码：{code}")
    if days not in (7, 30):
        raise HTTPException(status_code=400, detail="days 仅支持 7/30")
    name = ""
    try:
        quote = await service.get_quote(code, resolve_market(code))
        name = quote.name or ""
    except ProviderError:
        pass  # 资讯检索不依赖名称，拿不到也能按代码搜
    try:
        return await news_mod.get_stock_news(code, name, days=days, force=refresh)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc, "获取资讯失败") from exc


# ------------------------------------------------------------------ 券商研报

@router.get("/reports/{code}")
async def stock_reports(
    code: str,
    days: int = Query(365, description="时间范围（天）：30/90/365，0=全部"),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    """券商研报（同花顺个股页数据），按日期倒序：标题/机构/研究员/评级/日期。

    days 控制时间范围（近1月=30 / 近3月=90 / 近1年=365 / 全部=0），
    评级分布统计条与研报列表随范围联动。
    """
    code = normalize_code(code)
    # 防御性：路径上 {code} 可能被路由到任意字符串（旧的注册顺序 bug，
    # /ai/{code} 抢在 /ai/watchlist 前面），保证只接受 6 位数字股票代码，
    # 否则直接 404，避免把 "watchlist" 这种字面量当股票去请求所有行情源，
    # 触发「所有行情数据源均不可用」误报。
    if not code or not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=404, detail=f"非法的股票代码：{code}")
    if days not in (0, 30, 90, 365):
        raise HTTPException(status_code=400, detail="days 仅支持 0/30/90/365")
    try:
        return await reports_mod.get_reports(code, days=days, force=refresh)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc, "获取研报失败") from exc


# ------------------------------------------------------------------ AI 分析

@router.get("/ai/watchlist")
@router.post("/ai/watchlist")
async def ai_watchlist(refresh: bool = Query(False)) -> dict[str, Any]:
    """自选股批量 AI 摘要：优先读缓存，refresh=1 时用规则引擎快速重算并写入缓存。

    返回轻量摘要列表（action/confidence/reason/支撑压力/周期/引擎），供首页
    行内信号丸与总览卡片使用。规则引擎快照标记 is_brief，不会替代单股的
    完整 LLM 分析。
    """
    codes = await storage.a_watchlist_codes()
    if not codes:
        return {"items": [], "total": 0, "analyzed": 0, "refresh": refresh}

    summaries: list[dict[str, Any]] = []
    missing: list[str] = []

    # 先尽量读缓存，保证 GET 响应快
    for code in codes:
        cached = _cached_brief_report(code)
        if cached:
            summaries.append(_ai_summary_from_report(cached))
        else:
            missing.append(code)

    if refresh and missing:
        sem = asyncio.Semaphore(3)

        async def _one(code: str) -> dict[str, Any] | None:
            async with sem:
                try:
                    return await _generate_rule_summary(code)
                except Exception as exc:  # noqa: BLE001
                    log.warning("批量 AI 分析 %s 失败：%s", code, describe_exc(exc))
                    return None

        results = await asyncio.gather(*[_one(c) for c in missing], return_exceptions=True)
        for res in results:
            if isinstance(res, dict):
                summaries.append(_ai_summary_from_report(res))

    return {
        "items": summaries,
        "total": len(codes),
        "analyzed": len(summaries),
        "refresh": refresh,
    }


@router.post("/ai/{code}")
async def ai_analyze(code: str, refresh: bool = Query(False)) -> dict[str, Any]:
    code = normalize_code(code)
    # 防御性：路径上 {code} 可能被路由到任意字符串（旧的注册顺序 bug，
    # /ai/{code} 抢在 /ai/watchlist 前面），保证只接受 6 位数字股票代码，
    # 否则直接 404，避免把 "watchlist" 这种字面量当股票去请求所有行情源，
    # 触发「所有行情数据源均不可用」误报。
    if not code or not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=404, detail=f"非法的股票代码：{code}")

    async def _work() -> dict[str, Any]:
        # 等锁期间可能已被并发请求填充，二次检查缓存
        if not refresh:
            cached = _cached_report(code)
            if cached:
                return cached

        try:
            # 重建（缓存过期/强制刷新）时强制实时取数：行情、K线、资金、两融
            # 全部重新拉取，保证 AI 分析用的就是当下最新数据
            detail = await service.stock_detail(code, resolve_market(code), force=True)
        except ProviderError as exc:
            raise _fail(exc, "AI 分析取数失败") from exc

        # 资讯与研报各自会触发一次 LLM 解读，两者互不依赖：并发取，
        # 把这一段的墙钟从「资讯耗时 + 研报耗时」压到 max(两者)。
        # 任一失败都不阻塞主分析（return_exceptions 后按异常忽略）。
        news_items: list[dict[str, Any]] = []
        report_items: list[dict[str, Any]] = []
        report_dist: dict[str, int] = {}
        _name = detail["quote"].get("name", "")
        _news_res, _reports_res = await asyncio.gather(
            news_mod.get_stock_news(code, _name),
            reports_mod.get_reports(code, _name),
            return_exceptions=True,
        )
        if isinstance(_news_res, BaseException):
            log.info("AI 分析取资讯失败（忽略，不影响分析）：%s", describe_exc(_news_res))
        else:
            news_items = _news_res["items"]
        if isinstance(_reports_res, BaseException):
            log.info("AI 分析取研报失败（忽略，不影响分析）：%s", describe_exc(_reports_res))
        else:
            report_items = _reports_res["items"]
            report_dist = _reports_res.get("rating_dist") or {}

        result = await analysis.analyze(detail, news_items, report_items)
        quote = detail["quote"]
        report = {
            "code": code,
            "name": quote.get("name"),
            "board": quote.get("board"),
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "analysis": result["analysis"],
            "meta": {
                **result["meta"],
                "fingerprint": llmcfg.fingerprint(),
                "score_fp": scorecfg.fingerprint(),
                "schema_version": REPORT_SCHEMA_VERSION,
            },
            "status_tags": detail["status"]["tags"],
            # 研报面统计与关键研报预览（供前端结论下方单独展示）
            "report_sentiment": _sentiment_stats(report_items),
            "rating_dist": report_dist,
            "reports_preview": [
                {
                    "date": r.get("date", "")[:10],
                    "source": r.get("source", ""),
                    "rating": r.get("rating", ""),
                    "sentiment": (r.get("interpretation") or {}).get("sentiment", "中性"),
                    "title": r.get("title", ""),
                }
                for r in report_items[:5]
            ],
            "from_cache": False,
        }
        await storage.a_save_report(code, report)
        return report

    # 每股票单飞：并发点击同一只股票只打一次 LLM/取数，其余请求等待复用
    return await _with_ai_lock(code, _work)


async def _generate_rule_summary(code: str) -> dict[str, Any]:
    """用规则引擎快速生成单股轻量快照（不调用 LLM），用于首页批量刷新。"""
    detail = await service.stock_detail(code, resolve_market(code), force=False)
    result = analysis.rule_based(detail)
    quote = detail["quote"]
    report: dict[str, Any] = {
        "code": code,
        "name": quote.get("name"),
        "board": quote.get("board"),
        "price": quote.get("price"),
        "change_pct": quote.get("change_pct"),
        "analysis": result["analysis"],
        "meta": {
            **result["meta"],
            "fingerprint": llmcfg.fingerprint(),
            "score_fp": scorecfg.fingerprint(),
            "schema_version": REPORT_SCHEMA_VERSION,
            "is_brief": True,
        },
        "status_tags": detail["status"]["tags"],
        "report_sentiment": {"bull": 0, "bear": 0, "neutral": 0, "score": 0},
        "rating_dist": {},
        "reports_preview": [],
        "from_cache": False,
        "is_brief": True,
    }
    await storage.a_save_report(code, report)
    return report


