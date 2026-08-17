"""HTTP API 路由。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import analysis, llm, llmcfg, service, storage
from .config import settings
from .providers import ProviderError, registry
from .utils import normalize_code, resolve_market

log = logging.getLogger("api")
router = APIRouter(prefix="/api")


class AddWatchBody(BaseModel):
    code: str = Field(..., description="6 位股票代码")
    name: str | None = None
    board: str | None = None


class CodesBody(BaseModel):
    codes: list[str] = Field(default_factory=list)


class LLMConfigBody(BaseModel):
    enabled: bool = True
    vendor: str = "custom"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    json_mode: bool = True
    clear_key: bool = False


def _fail(exc: Exception, hint: str) -> HTTPException:
    log.warning("%s: %s", hint, exc)
    return HTTPException(status_code=503, detail=f"{hint}：{exc}")


# ------------------------------------------------------------------ AI 并发去重
# 同一股票并发触发分析时只执行一次 LLM 调用，其余请求等待并复用其结果。
class _AILock:
    __slots__ = ("lock", "waiters")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.waiters = 0


_ai_locks: dict[str, _AILock] = {}


async def _with_ai_lock(code: str, work: Callable[[], Awaitable[Any]]) -> Any:
    entry = _ai_locks.get(code)
    if entry is None:
        entry = _ai_locks[code] = _AILock()
    entry.waiters += 1
    try:
        async with entry.lock:
            return await work()
    finally:
        entry.waiters -= 1
        if entry.waiters <= 0 and _ai_locks.get(code) is entry:
            _ai_locks.pop(code, None)


def _cached_report(code: str) -> dict[str, Any] | None:
    """当日缓存命中；但 LLM 配置变化后旧缓存作废（如规则引擎降级结果 -> 已配置大模型）。"""
    cached = storage.get_report(code)
    if not cached:
        return None
    meta = cached.get("meta") or {}
    if meta.get("fingerprint") != llmcfg.fingerprint():
        return None
    return cached


# ------------------------------------------------------------------ 数据源健康自检
# 逐源实测各能力（会真实请求数据源，较慢），同一时刻只允许一次探测，避免刷新风暴。
_health_lock = asyncio.Lock()


@router.get("/health/check")
async def health_check() -> dict[str, Any]:
    from . import check_sources

    if _health_lock.locked():
        raise HTTPException(status_code=409, detail="已有自检正在进行，请稍候")
    async with _health_lock:
        try:
            return await asyncio.wait_for(check_sources.run_diagnostics(None), timeout=90)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="自检超时（>90s），请稍后重试")


# ------------------------------------------------------------------ 元信息

@router.get("/meta")
async def meta() -> dict[str, Any]:
    cfg = llmcfg.get_config()
    return {
        "app": settings.APP_NAME,
        "session": service.session_info(),
        "providers": registry().health(),
        "throttled_hosts": registry().throttled_hosts(),
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
    cfg = llmcfg.get_config()
    return {
        "enabled": cfg["enabled"],
        "vendor": cfg["vendor"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "api_key_set": bool(cfg.get("api_key")),
        "json_mode": cfg["json_mode"],
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
    llmcfg.save_config(body.model_dump(exclude_unset=True))
    cfg = llmcfg.get_config()
    return {
        "ok": True,
        "enabled": cfg["enabled"],
        "vendor": cfg["vendor"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "api_key_set": bool(cfg.get("api_key")),
        "json_mode": cfg["json_mode"],
        "engine": "llm" if llm.available() else "rule",
    }


@router.post("/llm/test")
async def llm_config_test(body: LLMConfigBody) -> dict[str, Any]:
    """用界面表单（未保存）的配置做一次最小调用。"""
    cfg = llmcfg.merge_pending(body.model_dump(exclude_unset=True))
    ok, message = await llm.test_connection(cfg)
    return {"ok": ok, "message": message}


@router.post("/llm/models")
async def llm_config_models(body: LLMConfigBody) -> dict[str, Any]:
    """从云端拉取模型列表（OpenAI 兼容 GET /models）。"""
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

    created = storage.add_watch(code, name, board)
    return {"ok": True, "created": created, "code": code, "name": name, "board": board}


@router.post("/watchlist/remove")
async def remove_watchlist(body: CodesBody) -> dict[str, Any]:
    """批量删除（需求 7.4）。"""
    removed = storage.remove_watch(body.codes)
    return {"ok": True, "removed": removed}


@router.post("/watchlist/order")
async def order_watchlist(body: CodesBody) -> dict[str, Any]:
    """拖拽排序落库（需求 3.2.2）。"""
    storage.reorder_watch(body.codes)
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


# ------------------------------------------------------------------ 详情

@router.get("/stock/{code}")
async def stock_detail(code: str, refresh: bool = Query(False)) -> dict[str, Any]:
    code = normalize_code(code)
    if not code:
        raise HTTPException(status_code=400, detail="股票代码不能为空")
    try:
        return await service.stock_detail(code, resolve_market(code), force=refresh)
    except ProviderError as exc:
        raise _fail(exc, "获取股票详情失败") from exc


@router.get("/quote/{code}")
async def stock_quote(code: str, refresh: bool = Query(False)) -> dict[str, Any]:
    """轻量行情（详情页自动刷新用）：只返回单只报价，不携带 K线/资金/两融历史。"""
    code = normalize_code(code)
    if not code:
        raise HTTPException(status_code=400, detail="股票代码不能为空")
    try:
        quote = await service.get_quote(code, resolve_market(code), force=refresh)
    except ProviderError as exc:
        raise _fail(exc, "获取行情失败") from exc
    return {"quote": quote.to_dict(), "session": service.session_info()}


# ------------------------------------------------------------------ AI 分析

@router.post("/ai/{code}")
async def ai_analyze(code: str, refresh: bool = Query(False)) -> dict[str, Any]:
    code = normalize_code(code)
    if not code:
        raise HTTPException(status_code=400, detail="股票代码不能为空")

    async def _work() -> dict[str, Any]:
        # 等锁期间可能已被并发请求填充，二次检查缓存
        if not refresh:
            cached = _cached_report(code)
            if cached:
                return cached

        try:
            detail = await service.stock_detail(code, resolve_market(code))
        except ProviderError as exc:
            raise _fail(exc, "AI 分析取数失败") from exc

        result = await analysis.analyze(detail)
        quote = detail["quote"]
        report = {
            "code": code,
            "name": quote.get("name"),
            "board": quote.get("board"),
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "analysis": result["analysis"],
            "meta": {**result["meta"], "fingerprint": llmcfg.fingerprint()},
            "status_tags": detail["status"]["tags"],
            "from_cache": False,
        }
        storage.save_report(code, report)
        return report

    # 每股票单飞：并发点击同一只股票只打一次 LLM/取数，其余请求等待复用
    return await _with_ai_lock(code, _work)
