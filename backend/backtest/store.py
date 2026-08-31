"""回测运行记录的落盘与索引。

目录结构（均在 `data/backtest_runs/`，已随 `data/` 一起 gitignore）：

    data/backtest_runs/
      index.json                     # 最近 N 次运行的摘要，前端历史列表读它
      <run_id>/
        meta.json                    # 状态 / 参数 / 进度 / 错误 / 起止时间
        trades.csv                   # 事件明细（事件研究）或成交明细（策略回测）
        summary.json                 # 事件级汇总
        tables.json                  # 前端要渲染的分档表等
        report.html                  # 独立看板（可在新窗口打开）
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = ROOT / "data" / "backtest_runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

INDEX_FILE = RUNS_DIR / "index.json"
INDEX_LIMIT = 30          # 索引只保留最近 30 次
KEEP_RUNS = 20            # 磁盘上保留最近 20 次的完整产物
_LOCK = threading.Lock()

# 进行中的运行状态（内存态，重启后丢失；meta.json 是持久态）
_RUNS: dict[str, dict[str, Any]] = {}


def new_run_id() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def run_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id


def create(strategy_id: str, strategy_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """新建一次运行记录（status=queued）。"""
    rid = new_run_id()
    meta = {
        "run_id": rid,
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "params": params,
        "status": "queued",          # queued / running / done / failed
        "progress": 0.0,
        "stage": "排队中",
        "error": None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": None,
        "has_report": False,
    }
    d = run_dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    _write_meta(rid, meta)
    with _LOCK:
        _RUNS[rid] = meta
    return meta


def _write_meta(run_id: str, meta: dict[str, Any]) -> None:
    meta["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    d = run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def update(run_id: str, **fields: Any) -> None:
    """更新运行状态（进度 / 阶段 / 状态 / 错误）。"""
    with _LOCK:
        meta = _RUNS.get(run_id)
        if meta is None:
            meta = _load_meta(run_id) or {}
            _RUNS[run_id] = meta
        meta.update(fields)
        _write_meta(run_id, meta)


def progress(run_id: str, pct: float, stage: str = "") -> None:
    update(run_id, progress=round(max(0.0, min(1.0, pct)), 4), stage=stage or "运行中",
           status="running")


def finish(run_id: str, result: dict[str, Any]) -> None:
    """写入完成态与产物，并刷新索引。"""
    d = run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)

    summary = result.get("summary") or {}
    (d / "summary.json").write_text(
        json.dumps(result.get("full_summary", summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tables = result.get("tables") or {}
    (d / "tables.json").write_text(
        json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if result.get("trades_csv"):
        shutil.move(str(result["trades_csv"]), str(d / "trades.csv"))
    report_src = result.get("report_html")
    if report_src and Path(report_src).exists():
        shutil.move(str(report_src), str(d / "report.html"))

    with _LOCK:
        meta = _RUNS.get(run_id) or _load_meta(run_id) or {}
        started = meta.get("started_at")
        meta.update({
            "status": "done",
            "progress": 1.0,
            "stage": "完成",
            "error": None,
            "has_report": bool(report_src),
            "duration_ms": int((time.time() - started) * 1000) if started else None,
        })
        _RUNS[run_id] = meta
        _write_meta(run_id, meta)
    _refresh_index(run_id, meta, summary)


def fail(run_id: str, error: str) -> None:
    with _LOCK:
        meta = _RUNS.get(run_id) or _load_meta(run_id) or {}
        meta.update({"status": "failed", "stage": "失败", "error": str(error)[:500]})
        _RUNS[run_id] = meta
        _write_meta(run_id, meta)
    _refresh_index(run_id, meta, {})


def _load_meta(run_id: str) -> dict[str, Any] | None:
    p = run_dir(run_id) / "meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def get_meta(run_id: str) -> dict[str, Any] | None:
    with _LOCK:
        meta = _RUNS.get(run_id)
    if meta:
        return meta
    return _load_meta(run_id)


def _refresh_index(run_id: str, meta: dict[str, Any], summary: dict[str, Any]) -> None:
    with _LOCK:
        items: list[dict[str, Any]] = []
        if INDEX_FILE.exists():
            try:
                items = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                items = []
        items = [it for it in items if it.get("run_id") != run_id]
        items.insert(0, {
            "run_id": run_id,
            "strategy_id": meta.get("strategy_id"),
            "strategy_name": meta.get("strategy_name"),
            "status": meta.get("status"),
            "created_at": meta.get("created_at"),
            "duration_ms": meta.get("duration_ms"),
            "total_events": summary.get("total_events"),
            "win_rate_pct": summary.get("win_rate_pct"),
            "avg_return_pct": summary.get("avg_return_pct"),
            "params": meta.get("params"),
        })
        items = items[:INDEX_LIMIT]
        INDEX_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    _gc()


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    if not INDEX_FILE.exists():
        return []
    try:
        items = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return items[:limit]


def load_result(run_id: str) -> dict[str, Any] | None:
    """读取一次已完成运行的完整结果（供前端渲染）。"""
    d = run_dir(run_id)
    summary_p = d / "summary.json"
    if not summary_p.exists():
        return None
    try:
        summary = json.loads(summary_p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    tables: dict[str, Any] = {}
    tp = d / "tables.json"
    if tp.exists():
        try:
            tables = json.loads(tp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            tables = {}
    # summary.json 存的是「full_summary」（可能含 meta/buckets 等嵌套），
    # 而 API / 前端要的是扁平的事件级统计；两者都在返回里给全。
    flat = summary.get("summary") if isinstance(summary.get("summary"), dict) else summary
    return {"summary": flat, "full_summary": summary, "tables": tables}


def load_trades(run_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """读取事件明细前 N 行（大表不全量下发）。"""
    import csv

    p = run_dir(run_id) / "trades.csv"
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for i, row in enumerate(reader):
            if i >= limit:
                break
            rows.append(row)
    return rows


def report_path(run_id: str) -> Path | None:
    p = run_dir(run_id) / "report.html"
    return p if p.exists() else None


def delete_run(run_id: str) -> bool:
    d = run_dir(run_id)
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    with _LOCK:
        _RUNS.pop(run_id, None)
        if INDEX_FILE.exists():
            try:
                items = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                items = []
            items = [it for it in items if it.get("run_id") != run_id]
            INDEX_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    return True


def _gc(keep: int = KEEP_RUNS) -> None:
    """只保留最近 keep 次的完整产物，避免磁盘无限增长。"""
    dirs = [p for p in RUNS_DIR.iterdir() if p.is_dir()]
    if len(dirs) <= keep:
        return
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in dirs[keep:]:
        shutil.rmtree(p, ignore_errors=True)


def is_busy() -> str | None:
    """是否已有运行中的回测（返回 run_id，没有则 None）。"""
    with _LOCK:
        for rid, meta in _RUNS.items():
            if meta.get("status") in ("queued", "running"):
                return rid
    # 兜底：进程重启后内存态丢失，扫磁盘上残留的 running 记录
    for p in RUNS_DIR.iterdir():
        if not p.is_dir():
            continue
        meta = _load_meta(p.name)
        if meta and meta.get("status") in ("queued", "running"):
            meta["status"] = "failed"
            meta["error"] = "服务重启，本次运行已中断"
            _write_meta(p.name, meta)
    return None
