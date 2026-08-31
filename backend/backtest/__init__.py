"""回测模块：把策略回测从散装脚本收进独立子包，供 API / CLI 共用。

对外入口：
    from backend import backtest

    backtest.list_strategies()          # 策略清单（含参数 schema）
    await backtest.submit(sid, params)  # 提交运行，返回 run_id（后台执行）
    backtest.get_run(run_id)            # 查询状态 / 进度 / 结果
    backtest.list_runs()                # 历史运行列表
    backtest.delete_run(run_id)

命令行：
    python -m backend.backtest --list
    python -m backend.backtest --strategy score_threshold --limit 400
"""
from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any

from . import registry as _registry
from . import store as _store

__all__ = [
    "list_strategies", "submit", "get_run", "list_runs", "delete_run",
    "run_result", "run_trades", "report_path", "busy_run",
]

# 在跑的任务句柄（用于取消 / 排查）
_TASKS: dict[str, asyncio.Task] = {}


def list_strategies() -> list[dict[str, Any]]:
    return _registry.all_strategies()


def busy_run() -> str | None:
    """当前是否有运行中的回测（并发保护）。"""
    return _store.is_busy()


def submit(strategy_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """提交一次回测：先落盘 queued 记录，再挂后台任务，立即返回 run_id。"""
    strat = _registry.get(strategy_id)
    if strat is None:
        raise KeyError(f"未知策略: {strategy_id}")
    running = busy_run()
    if running:
        raise RuntimeError(f"已有回测在运行中（{running}），请等待其完成")
    meta = _store.create(strategy_id, strat.name, _clean_params(strat, params))
    task = asyncio.create_task(_execute(meta["run_id"], strategy_id, params))
    _TASKS[meta["run_id"]] = task
    return meta


def _clean_params(strat: _registry.Strategy, params: dict[str, Any]) -> dict[str, Any]:
    """只保留 schema 里声明过的键，避免前端塞入脏字段落盘。"""
    keys = {f["key"] for f in strat.schema}
    return {k: v for k, v in (params or {}).items() if k in keys}


async def _execute(run_id: str, strategy_id: str, params: dict[str, Any]) -> None:
    strat = _registry.get(strategy_id)
    if strat is None:
        _store.fail(run_id, f"未知策略: {strategy_id}")
        return

    def on_progress(pct: float, stage: str) -> None:
        _store.progress(run_id, pct, stage)

    _store.update(run_id, status="running", started_at=time.time(), stage="启动")
    try:
        result = await strat.runner(params, on_progress)
        _store.finish(run_id, result)
    except asyncio.CancelledError:
        _store.fail(run_id, "已取消")
        raise
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _store.fail(run_id, str(exc))
    finally:
        _TASKS.pop(run_id, None)


def get_run(run_id: str) -> dict[str, Any] | None:
    """运行状态；已完成时附带 summary 与 tables。"""
    meta = _store.get_meta(run_id)
    if meta is None:
        return None
    out = dict(meta)
    if meta.get("status") == "done":
        res = _store.load_result(run_id)
        if res:
            out["summary"] = res["summary"]
            out["tables"] = res["tables"]
    return out


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    return _store.list_runs(limit)


def delete_run(run_id: str) -> bool:
    task = _TASKS.get(run_id)
    if task and not task.done():
        task.cancel()
    return _store.delete_run(run_id)


def run_result(run_id: str) -> dict[str, Any] | None:
    return _store.load_result(run_id)


def run_trades(run_id: str, limit: int = 500) -> list[dict[str, Any]]:
    return _store.load_trades(run_id, limit)


def report_path(run_id: str) -> str | None:
    p = _store.report_path(run_id)
    return str(p) if p else None
