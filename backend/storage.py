"""SQLite 持久化：自选股列表 + 当日 AI 分析记录。

放在挂载卷 DATA_DIR 下，容器重启/重建后自选股不丢失（需求 7.2）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

from .config import settings
from .utils import full_code, normalize_code, resolve_market, today_str

log = logging.getLogger("storage")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _init(_conn)
        run_migrations(_conn)
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


# ------------------------------------------------------------------ schema 迁移

# 为什么需要这套东西：_init 用的是 CREATE TABLE IF NOT EXISTS，表一旦存在就静默跳过，
# 于是「给已有表加字段」在代码里没有任何执行入口——想加持仓成本只能人肉敲 ALTER。
# 这里改用 SQLite 标准的 PRAGMA user_version 记录 schema 版本，配一份有序迁移清单，
# 让建表之后的每次结构变更都有地方登记、有地方执行。
#
# 约定：
#   - _init() 建出来的三张表 = version 1，同时也是所有存量老库的结构；
#   - 每条迁移写成 (目标版本号, SQL)。SQL 可以是一条 ALTER，也可以是 executescript
#     能跑的多语句（建索引 / 建表 / 回填数据）；
#   - 只许往后追加，不许改或删已发布的迁移——线上库可能已经跑过它们，改了就对不上账。
SCHEMA_VERSION = 1

MIGRATIONS: list[tuple[int, str]] = [
    # 加字段时按此格式追加，并把 SCHEMA_VERSION 同步 +1：
    # (2, "ALTER TABLE watchlist ADD COLUMN cost_price REAL"),
]


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA 不接受参数绑定，只能拼进字符串；version 全部来自本模块的迁移清单，
    # 且过一遍 int() 收敛类型，不存在注入面。
    conn.execute(f"PRAGMA user_version={int(version)}")


def run_migrations(
    conn: sqlite3.Connection,
    migrations: list[tuple[int, str]] | None = None,
    target_version: int | None = None,
) -> int:
    """把 schema 升到 target_version（默认 SCHEMA_VERSION），返回升级后的版本号。

    幂等：只跑版本号 > 当前 user_version 的迁移，跑过的不重复执行。
    migrations / target_version 两个参数平时不用，是留给测试注入迁移用的——
    不注入就没法在不动生产 schema 的前提下端到端验证这套机制真的会改表。
    """
    steps = MIGRATIONS if migrations is None else migrations
    target = SCHEMA_VERSION if target_version is None else target_version

    current = _user_version(conn)
    # user_version=0 说明这个库建于引入版本号之前，它的表结构正是 _init 建出来的
    # version 1 的样子（_init 刚才已把它补齐），直接盖章，不必补建。
    if current == 0:
        _set_user_version(conn, 1)
        current = 1
    if current > target:
        # 代码回滚到旧版本时会走到这里：库比代码新，硬跑会重复执行已应用的迁移。
        # 只告警不动数据——用户数据比 schema 版本号重要。
        log.warning(
            "数据库 schema 版本(%s) 高于当前代码支持的最高版本(%s)，跳过迁移",
            current, target,
        )
        return current

    for version, sql in sorted(steps):
        if version <= current or version > target:
            continue
        try:
            conn.executescript(sql)
        except sqlite3.Error as exc:
            # 不吞异常：半途停下比带着残缺 schema 继续服务更安全。
            # 此处 user_version 还没动，下次启动会重试同一条迁移；单条 ALTER 失败
            # 不留残留（SQLite 单语句 DDL 自带事务），可以安全重试。写成多语句的
            # 迁移则要自己保证可重入（如用 IF NOT EXISTS）。
            conn.rollback()
            raise RuntimeError(
                f"数据库迁移失败：version {current} -> {version}，本次未提交。SQL: {sql.strip()}"
            ) from exc
        _set_user_version(conn, version)
        log.info("数据库 schema 升级：%s -> %s", current, version)
        current = version
    conn.commit()
    return current


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


def watched_codes() -> set[str]:
    """一次取回全部自选股代码，供批量判定使用。

    搜索结果 / 热门榜要给每一行标注是否已自选，逐行查会退化成 N+1 次查询。
    自选股规模很小，整表读进内存做集合判定更划算。
    """
    with _lock:
        rows = _connect().execute("SELECT code FROM watchlist").fetchall()
    return {r["code"] for r in rows}


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
