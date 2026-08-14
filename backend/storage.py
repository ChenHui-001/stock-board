"""SQLite 持久化：自选股列表 + 当日 AI 分析记录。

放在挂载卷 DATA_DIR 下，容器重启/重建后自选股不丢失（需求 7.2）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from .config import settings
from .utils import full_code, normalize_code, resolve_market, today_str

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _init(_conn)
    return _conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            code      TEXT PRIMARY KEY,
            market    TEXT NOT NULL,
            name      TEXT,
            board     TEXT,
            sort_no   INTEGER NOT NULL DEFAULT 0,
            added_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS ai_report (
            code        TEXT NOT NULL,
            trade_date  TEXT NOT NULL,
            payload     TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (code, trade_date)
        );
        CREATE TABLE IF NOT EXISTS kv (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.commit()


def init_db() -> None:
    with _lock:
        _connect()


# ------------------------------------------------------------------ 自选股

def list_watchlist() -> list[dict[str, Any]]:
    with _lock:
        rows = _connect().execute(
            "SELECT code, market, name, board, sort_no FROM watchlist ORDER BY sort_no ASC, added_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def watchlist_codes() -> list[str]:
    return [full_code(r["code"], r["market"]) for r in list_watchlist()]


def add_watch(code: str, name: str | None = None, board: str | None = None) -> bool:
    """返回 True 表示新增，False 表示已存在。"""
    code = normalize_code(code)
    if not code:
        raise ValueError("股票代码不能为空")
    market = resolve_market(code)
    with _lock:
        conn = _connect()
        exists = conn.execute("SELECT 1 FROM watchlist WHERE code=?", (code,)).fetchone()
        if exists:
            if name or board:
                conn.execute(
                    "UPDATE watchlist SET name=COALESCE(?,name), board=COALESCE(?,board) WHERE code=?",
                    (name, board, code),
                )
                conn.commit()
            return False
        max_no = conn.execute("SELECT COALESCE(MAX(sort_no),0) AS m FROM watchlist").fetchone()["m"]
        conn.execute(
            "INSERT INTO watchlist (code, market, name, board, sort_no) VALUES (?,?,?,?,?)",
            (code, market, name, board, max_no + 1),
        )
        conn.commit()
    return True


def remove_watch(codes: list[str]) -> int:
    normalized = [normalize_code(c) for c in codes if normalize_code(c)]
    if not normalized:
        return 0
    placeholders = ",".join("?" * len(normalized))
    with _lock:
        conn = _connect()
        cur = conn.execute(f"DELETE FROM watchlist WHERE code IN ({placeholders})", normalized)
        conn.commit()
        return cur.rowcount


def reorder_watch(codes: list[str]) -> None:
    """按传入顺序重排（拖拽排序落库）。"""
    normalized = [normalize_code(c) for c in codes if normalize_code(c)]
    with _lock:
        conn = _connect()
        for idx, code in enumerate(normalized, start=1):
            conn.execute("UPDATE watchlist SET sort_no=? WHERE code=?", (idx, code))
        conn.commit()


def update_meta(code: str, name: str | None, board: str | None) -> None:
    code = normalize_code(code)
    if not (name or board):
        return
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE watchlist SET name=COALESCE(?,name), board=COALESCE(?,board) WHERE code=?",
            (name, board, code),
        )
        conn.commit()


def is_watched(code: str) -> bool:
    code = normalize_code(code)
    with _lock:
        return _connect().execute("SELECT 1 FROM watchlist WHERE code=?", (code,)).fetchone() is not None


# ------------------------------------------------------------------ 键值配置

def get_kv(key: str) -> str | None:
    with _lock:
        row = _connect().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_kv(key: str, value: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def delete_kv(key: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM kv WHERE key=?", (key,))
        conn.commit()


def get_llm_config() -> dict[str, Any]:
    raw = get_kv("llm_config")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def set_llm_config(cfg: dict[str, Any]) -> None:
    set_kv("llm_config", json.dumps(cfg, ensure_ascii=False))


def clear_llm_config() -> None:
    delete_kv("llm_config")


# ------------------------------------------------------------------ AI 记录

def save_report(code: str, payload: dict[str, Any]) -> None:
    code = normalize_code(code)
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO ai_report (code, trade_date, payload) VALUES (?,?,?) "
            "ON CONFLICT(code, trade_date) DO UPDATE SET payload=excluded.payload, "
            "created_at=datetime('now','localtime')",
            (code, today_str(), json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()


def get_report(code: str) -> dict[str, Any] | None:
    """只取当日记录，次日自动失效（需求 6.4）。"""
    code = normalize_code(code)
    with _lock:
        row = _connect().execute(
            "SELECT payload, created_at FROM ai_report WHERE code=? AND trade_date=?",
            (code, today_str()),
        ).fetchone()
    if not row:
        return None
    data = json.loads(row["payload"])
    data["cached_at"] = row["created_at"]
    data["from_cache"] = True
    return data


def purge_old_reports(keep_days: int = 7) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "DELETE FROM ai_report WHERE trade_date < date('now','localtime',?)",
            (f"-{keep_days} day",),
        )
        conn.commit()
