"""回测模块 HTTP 路由（`backend/backtest` 子包的对外接口）。

所有接口挂在 `/api/backtest` 下：
    GET    /api/backtest/strategies        策略清单（含参数 schema）
    POST   /api/backtest/run               提交一次运行，立即返回 run_id
    GET    /api/backtest/run/{id}          查状态 / 进度 / 结果（前端轮询）
    GET    /api/backtest/runs              历史运行列表
    DELETE /api/backtest/run/{id}          删除一次运行产物
    GET    /api/backtest/run/{id}/trades   事件明细（分页，默认 300 行）
    GET    /api/backtest/run/{id}/report   独立看板 HTML（可直接新窗口打开）

回测耗时通常在几十秒到几分钟，因此采用「提交 + 轮询」而不是同步等待：
Run 的持久态在 `data/backtest_runs/<id>/meta.json`，进程重启后仍可查询。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import backtest as bt

log = logging.getLogger("api.backtest")

router = APIRouter(prefix="/backtest")   # 挂到主 router（已带 /api）后即 /api/backtest/*


class RunBody(BaseModel):
    strategy_id: str = Field(..., description="策略 id，从 /strategies 取")
    params: dict[str, Any] = Field(default_factory=dict)


@router.get("/strategies")
async def strategies() -> dict[str, Any]:
    """策略清单：id / 名称 / 说明 / 参数 schema / 口径限制。

    前端按 schema 动态渲染参数表单，新增策略不需要改前端。
    """
    items = bt.list_strategies()
    return {"strategies": items, "running": bt.busy_run()}


@router.post("/run")
async def submit_run(body: RunBody) -> dict[str, Any]:
    """提交一次回测。回测在后台任务执行，立即返回 run_id 供轮询。

    同一时刻只允许一个回测在跑（数据源配额与 CPU 都有限），已有任务时返回 409。
    """
    try:
        meta = bt.submit(body.strategy_id, body.params or {})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"未知策略：{exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    log.info("回测提交 %s strategy=%s", meta["run_id"], body.strategy_id)
    return meta


@router.get("/runs")
async def list_runs(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """历史运行列表（最近 N 次，从索引文件读）。"""
    return {"runs": bt.list_runs(limit), "running": bt.busy_run()}


@router.get("/run/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    """查询运行状态。status=done 时一并返回 summary 与 tables。"""
    meta = bt.get_run(run_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return meta


@router.delete("/run/{run_id}")
async def remove_run(run_id: str) -> dict[str, Any]:
    """删除一次运行的产物（含看板与 CSV）。"""
    if not bt.delete_run(run_id):
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return {"ok": True, "run_id": run_id}


@router.get("/run/{run_id}/trades")
async def get_trades(run_id: str, limit: int = Query(300, ge=1, le=5000)) -> dict[str, Any]:
    """事件明细（抽样下发，避免大表把浏览器打爆）。"""
    rows = bt.run_trades(run_id, limit)
    return {"rows": rows, "limit": limit}


@router.get("/run/{run_id}/report", response_class=HTMLResponse)
async def get_report(run_id: str) -> Any:
    """独立看板 HTML：自包含（数据内联），可直接新窗口打开或下载。"""
    path = bt.report_path(run_id)
    if not path:
        raise HTTPException(status_code=404, detail="该运行没有可用看板（可能未完成或失败）")
    return FileResponse(path, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})
