"""storage 的 schema 迁移机制 + 批量写 / 异步封装。

为什么单独一个文件：迁移是会直接改库结构的操作，一旦写错就是毁数据，
值得有一组专门的用例盯着「幂等 / 顺序 / 老库升级 / 失败可重试」这四件事。
后半段的 update_meta_batch 与 a_xxx 封装是 P0-2（消除同步 sqlite 阻塞
事件循环）的产物，与迁移同属 storage 的连接/锁设计，放一起便于维护。

本文件不覆盖 storage 的常规增删改查，只覆盖迁移机制与并发相关契约。

隔离：所有用例都走 isolated_db fixture，库文件落在 tmp_path 下。
conftest 已把 DATA_DIR 指向 mkdtemp，因此不会碰到真实的 data/board.db。
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from backend import storage
from backend.config import settings

# version 1 的原始建表语句，抄自引入迁移机制之前的 storage._init()。
# 这里手工抄一份而不是直接调 _init()：老库升级的用例要是拿被测代码来搭自己的
# 前置条件，就成了「自己验自己」，测不出来真问题。
LEGACY_V1_DDL = """
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

V1_WATCHLIST_COLUMNS = {"code", "market", "name", "board", "sort_no", "added_at"}


def _pragma_version(conn: sqlite3.Connection) -> int:
    """直读 PRAGMA user_version，不走 storage._user_version——

    万一那个 helper 自己读错了，断言不该跟着一起被骗过去。
    """
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch) -> Path:
    """把 storage 的全局连接指向 tmp_path 下的独立库文件，用完自动收摊。

    storage._conn 是模块级单例，指向的是 settings.db_path；换库必须先把单例清掉，
    否则 storage 会继续复用上一个测试打开的连接，用例之间互相串数据。
    """
    # 防御：DATA_DIR 必须是 conftest 建出来的临时目录，别把真实库改了
    assert "board-pytest-" in str(settings.DATA_DIR), f"DATA_DIR 隔离失效：{settings.DATA_DIR}"

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    if storage._conn is not None:
        storage._conn.close()
        storage._conn = None
    yield tmp_path / "board.db"
    if storage._conn is not None:
        storage._conn.close()
        storage._conn = None


def _make_legacy_db(db_path: Path) -> None:
    """造一个「引入版本号之前」的库：v1 表结构 + 若干数据 + user_version 未写过(0)。"""
    legacy = sqlite3.connect(db_path)
    legacy.row_factory = sqlite3.Row
    legacy.executescript(LEGACY_V1_DDL)
    legacy.execute(
        "INSERT INTO watchlist (code, market, name, board, sort_no) VALUES (?,?,?,?,?)",
        ("600519", "SH", "贵州茅台", "主板", 1),
    )
    legacy.execute("INSERT INTO kv (key, value) VALUES (?,?)", ("llm_config", '{"model":"x"}'))
    legacy.commit()
    assert _pragma_version(legacy) == 0, "前置条件不成立：老库不应已有版本号"
    legacy.close()


# ------------------------------------------------------------------ 全新库

def test_fresh_db_is_stamped_at_schema_version(isolated_db: Path) -> None:
    conn = storage._connect()

    assert _pragma_version(conn) == storage.SCHEMA_VERSION
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"watchlist", "ai_report", "kv"} <= tables
    # 机制本身不引入任何业务字段，建出来的就是 version 1 的样子
    assert _columns(conn, "watchlist") == V1_WATCHLIST_COLUMNS


def test_run_migrations_is_idempotent(isolated_db: Path) -> None:
    conn = storage._connect()
    baseline = _pragma_version(conn)

    # 同一条连接上反复跑：不报错、版本号不动
    for _ in range(3):
        assert storage.run_migrations(conn) == baseline
    assert _pragma_version(conn) == baseline

    # 走完整入口反复初始化（等价于进程反复重启）也不重复执行
    for _ in range(3):
        storage.init_db()
    assert _pragma_version(conn) == baseline


# ------------------------------------------------------------------ 老库升级

def test_legacy_db_is_stamped_and_keeps_data(isolated_db: Path) -> None:
    """存量老库（user_version=0）接上新版代码后能正确识别为 version 1，数据不丢。"""
    _make_legacy_db(isolated_db)

    conn = storage._connect()

    assert _pragma_version(conn) == storage.SCHEMA_VERSION
    assert _columns(conn, "watchlist") == V1_WATCHLIST_COLUMNS
    row = conn.execute("SELECT code, name, sort_no FROM watchlist").fetchone()
    assert dict(row) == {"code": "600519", "name": "贵州茅台", "sort_no": 1}
    # 业务读路径照常工作（升级没把现有功能搞坏）
    assert storage.get_kv("llm_config") == '{"model":"x"}'
    assert storage.is_watched("600519") is True


def test_legacy_db_upgrades_through_injected_migration(isolated_db: Path, monkeypatch) -> None:
    """最接近真实场景的一条：老库文件 + 已带迁移的新代码，一次启动直接升到位。

    把 MIGRATIONS 和 SCHEMA_VERSION 一起换成「未来」的版本，走 _connect() 的真实
    调用链（_init -> run_migrations），也就是后续加持仓成本字段时线上会走的那条路。
    只有这条能证明「老库升级」不是走过场——空迁移清单下升级只改了个版本号，
    看不出执行器到底会不会执行 SQL。
    """
    _make_legacy_db(isolated_db)
    monkeypatch.setattr(
        storage, "MIGRATIONS", [(2, "ALTER TABLE watchlist ADD COLUMN note TEXT")]
    )
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 2)

    conn = storage._connect()

    assert _pragma_version(conn) == 2
    assert _columns(conn, "watchlist") == V1_WATCHLIST_COLUMNS | {"note"}
    # 老数据还在，新字段是 NULL 而不是把表重建掉
    row = conn.execute("SELECT code, name, note FROM watchlist").fetchone()
    assert dict(row) == {"code": "600519", "name": "贵州茅台", "note": None}


# ------------------------------------------------------------------ 执行器本身

def test_migration_really_alters_table_structure(isolated_db: Path) -> None:
    """注入一条真实 ALTER，验证执行器确实会改表结构，而不只是把版本号 +1。

    用 run_migrations 的参数注入而非直接改 storage.MIGRATIONS，这样既验证了执行器，
    又不会给生产库留下一个没人用的 note 字段。
    """
    conn = storage._connect()
    assert "note" not in _columns(conn, "watchlist")
    conn.execute(
        "INSERT INTO watchlist (code, market, name, sort_no) VALUES (?,?,?,?)",
        ("000001", "SZ", "平安银行", 1),
    )
    conn.commit()

    storage.run_migrations(
        conn,
        migrations=[(2, "ALTER TABLE watchlist ADD COLUMN note TEXT")],
        target_version=2,
    )

    # PRAGMA table_info 直读：列是真的加进去了
    info = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(watchlist)")}
    assert info["note"] == "TEXT"
    assert _pragma_version(conn) == 2

    # 已有数据没被冲掉，且新字段能读写
    assert storage.list_watchlist() == [
        {"code": "000001", "market": "SZ", "name": "平安银行", "board": None, "sort_no": 1}
    ]
    conn.execute("UPDATE watchlist SET note=? WHERE code=?", ("观察仓", "000001"))
    conn.commit()
    assert conn.execute(
        "SELECT note FROM watchlist WHERE code='000001'"
    ).fetchone()["note"] == "观察仓"

    # 升过之后再跑不会重复加列（重复 ADD COLUMN 会报 duplicate column name）
    storage.run_migrations(
        conn,
        migrations=[(2, "ALTER TABLE watchlist ADD COLUMN note TEXT")],
        target_version=2,
    )
    assert _pragma_version(conn) == 2
    assert [c for c in _columns(conn, "watchlist") if c == "note"] == ["note"]


def test_migrations_run_in_version_order(isolated_db: Path) -> None:
    """乱序写进清单的迁移要按版本号升序执行，否则依赖关系会崩。"""
    conn = storage._connect()

    # 故意乱序：3 写在 2 前面；且 3 依赖 2 建出来的列（回填），顺序错了必然报错
    storage.run_migrations(
        conn,
        migrations=[
            (3, "UPDATE watchlist SET note = name"),
            (2, "ALTER TABLE watchlist ADD COLUMN note TEXT"),
        ],
        target_version=3,
    )

    assert _pragma_version(conn) == 3
    assert {"note"} <= _columns(conn, "watchlist")


def test_failed_migration_reports_version_and_can_retry(isolated_db: Path) -> None:
    """迁移挂了要能一眼看出是哪一条，且不能留下「跳过去」的假象。"""
    conn = storage._connect()
    baseline = _pragma_version(conn)

    with pytest.raises(RuntimeError) as exc:
        storage.run_migrations(
            conn,
            migrations=[(2, "ALTER TABLE nosuchtable ADD COLUMN x TEXT")],
            target_version=2,
        )
    message = str(exc.value)
    assert "version 1 -> 2" in message, message
    assert "nosuchtable" in message, message

    # 版本号没动 -> 下次启动会重试同一条，不会被当成「已执行」跳过
    assert _pragma_version(conn) == baseline

    # 修好 SQL 后重试能成功：说明失败确实没留下半成品
    storage.run_migrations(
        conn,
        migrations=[(2, "ALTER TABLE watchlist ADD COLUMN note TEXT")],
        target_version=2,
    )
    assert _pragma_version(conn) == 2
    assert "note" in _columns(conn, "watchlist")


def test_db_newer_than_code_is_left_alone(isolated_db: Path) -> None:
    """代码回滚到旧版本时，不能把已经跑过的迁移再跑一遍。"""
    conn = storage._connect()
    conn.execute("PRAGMA user_version=99")   # 库比代码新
    conn.commit()

    storage.run_migrations(
        conn,
        migrations=[(2, "ALTER TABLE watchlist ADD COLUMN note TEXT")],
        target_version=2,
    )

    assert _pragma_version(conn) == 99
    assert "note" not in _columns(conn, "watchlist")


# ------------------------------------------------------------------ 批量回写

def _seed_watchlist(n: int) -> None:
    for i in range(n):
        storage.add_watch(f"{600000 + i:06d}", f"股票{i}", None)


def test_update_meta_batch_applies_all_rows(isolated_db: Path) -> None:
    _seed_watchlist(3)
    changed = storage.update_meta_batch(
        [("600000", "新名字A", "板块A"), ("600001", "新名字B", "板块B")]
    )

    assert changed == 2
    rows = {r["code"]: dict(r) for r in storage.list_watchlist()}
    assert rows["600000"]["name"] == "新名字A" and rows["600000"]["board"] == "板块A"
    assert rows["600001"]["name"] == "新名字B" and rows["600001"]["board"] == "板块B"
    # 没传的行不受影响
    assert rows["600002"]["name"] == "股票2" and rows["600002"]["board"] is None


def test_update_meta_batch_keeps_old_value_on_none(isolated_db: Path) -> None:
    """COALESCE 语义：传 None 表示「不改动」，不能把已有值清成空。"""
    _seed_watchlist(1)
    storage.update_meta("600000", "原名", "原板块")

    storage.update_meta_batch([("600000", None, "新板块")])
    row = storage.list_watchlist()[0]
    assert (row["name"], row["board"]) == ("原名", "新板块")

    storage.update_meta_batch([("600000", "新名", None)])
    row = storage.list_watchlist()[0]
    assert (row["name"], row["board"]) == ("新名", "新板块")


def test_update_meta_batch_matches_row_by_row_semantics(isolated_db: Path) -> None:
    """攒批的最终库状态必须与「在循环里逐条 update_meta」完全一致。

    这是把 service.watchlist_board 的 N+1 写改成攒批的前提：调用方只是把
    调用攒成列表，不允许出现任何行为差异（含同一行被回写两次的场景）。
    """
    _seed_watchlist(4)
    # 同一行先回写名称、再回写板块——正是 watchlist_board 里会发生的情况
    batched: list[tuple[str, str | None, str | None]] = [
        ("600000", "名A", "板A"),
        ("600000", None, "板A2"),
        ("600001", "名B", None),
        ("600003", None, "板D"),
    ]
    storage.update_meta_batch(batched)
    batch_state = [(r["code"], r["name"], r["board"]) for r in storage.list_watchlist()]

    # 换一个干净库，用老的逐条调用方式重放同一批更新
    storage.remove_watch([r[0] for r in batch_state])
    _seed_watchlist(4)
    for code, name, board in batched:
        storage.update_meta(code, name, board)
    loop_state = [(r["code"], r["name"], r["board"]) for r in storage.list_watchlist()]

    assert batch_state == loop_state


class _CountingConn:
    """统计 commit 次数的连接代理。

    sqlite3.Connection.commit 是只读属性，monkeypatch 打不上去，只能整体代理
    storage 的单例连接。除 commit 外一律透传给真实连接，不影响被测行为。
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1
        self._real.commit()

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def test_update_meta_batch_is_one_transaction(isolated_db: Path, monkeypatch) -> None:
    """批量回写的收益就在这：50 行只 commit 1 次，而不是 50 次。

    直接数 commit 次数——每次 commit 都要落一次 WAL，这才是逐行写的真实成本。
    """
    _seed_watchlist(50)
    proxy = _CountingConn(storage._connect())
    monkeypatch.setattr(storage, "_conn", proxy)

    rows = [(f"{600000 + i:06d}", f"名{i}", None) for i in range(50)]
    assert storage.update_meta_batch(rows) == 50
    assert proxy.commits == 1, f"批量写应只 commit 1 次，实际 {proxy.commits} 次"

    # 对照：同样 50 行逐条写，就是 50 次 commit（这就是被消除掉的开销）
    proxy.commits = 0
    for code, name, _board in rows:
        storage.update_meta(code, None, f"板{name}")
    assert proxy.commits == 50


def test_update_meta_batch_empty_input_is_noop(isolated_db: Path) -> None:
    """空列表不该报错也不该开事务——看板经常一只股票都不用回写。"""
    _seed_watchlist(1)
    assert storage.update_meta_batch([]) == 0
    assert storage.update_meta_batch([("", "名", "板")]) == 0
    assert storage.list_watchlist()[0]["name"] == "股票0"


# ------------------------------------------------------------------ 异步封装

def test_async_wrappers_are_coroutine_functions() -> None:
    """调用点改成 await 之后如果忘了加 async，这里会先炸。"""
    for name in ("a_list_watchlist", "a_add_watch", "a_update_meta_batch", "a_save_report"):
        assert asyncio.iscoroutinefunction(getattr(storage, name)), name


@pytest.mark.asyncio
async def test_async_wrapper_runs_off_the_event_loop(isolated_db: Path, monkeypatch) -> None:
    """a_xxx 必须真的把 DB 操作丢到别的线程，否则等于没解决阻塞问题。

    直接记录同步函数实际运行在哪个线程：如果还在主线程（事件循环所在线程），
    说明封装失效，sqlite 仍会阻塞整个 loop。
    """
    _seed_watchlist(2)
    seen: list[str] = []
    real = storage.list_watchlist

    def spy() -> list[dict[str, object]]:
        seen.append(threading.current_thread().name)
        return real()

    monkeypatch.setattr(storage, "list_watchlist", spy)

    rows = await storage.a_list_watchlist()

    assert len(rows) == 2
    assert seen == ["asyncio_0"] or seen[0].startswith("asyncio"), (
        f"DB 操作应跑在 asyncio 线程池里，实际线程：{seen}"
    )
    assert threading.current_thread().name != seen[0]


@pytest.mark.asyncio
async def test_async_wrappers_return_same_as_sync(isolated_db: Path) -> None:
    """封装只是换线程，不能改变任何语义或返回值。"""
    _seed_watchlist(2)

    assert await storage.a_list_watchlist() == storage.list_watchlist()
    assert await storage.a_watchlist_codes() == storage.watchlist_codes()
    assert await storage.a_watched_codes() == storage.watched_codes()
    assert await storage.a_is_watched("600000") is True
    assert await storage.a_is_watched("999999") is False
    assert await storage.a_add_watch("000001", "平安银行", "银行") is True
    assert await storage.a_add_watch("000001") is False          # 已存在
    assert await storage.a_get_kv("nope") is None

    await storage.a_update_meta("600000", "改名", "改板块")
    row = storage.list_watchlist()[0]
    assert (row["name"], row["board"]) == ("改名", "改板块")

    assert await storage.a_update_meta_batch([("600001", "批改名", None)]) == 1
    assert storage.list_watchlist()[1]["name"] == "批改名"

    await storage.a_save_report("600000", {"analysis": {"advice": {"action": "观望"}}})
    assert await storage.a_get_kv("x") is None
    cached = storage.get_report("600000")
    assert cached is not None and cached["from_cache"] is True

    assert await storage.a_remove_watch(["600000"]) == 1
    assert "600000" not in await storage.a_watched_codes()


# a_xxx -> (被包装的同步函数名, 调用参数)。新增封装时同步补一行，漏补会在这里被抓出来。
_ASYNC_WRAPPER_TABLE: list[tuple[str, str, tuple[object, ...]]] = [
    ("a_list_watchlist", "list_watchlist", ()),
    ("a_watchlist_codes", "watchlist_codes", ()),
    ("a_add_watch", "add_watch", ("000001",)),
    ("a_remove_watch", "remove_watch", (["000001"],)),
    ("a_reorder_watch", "reorder_watch", (["000001"],)),
    ("a_update_meta", "update_meta", ("000001", None, "板")),
    ("a_update_meta_batch", "update_meta_batch", ([("000001", None, "板")],)),
    ("a_is_watched", "is_watched", ("000001",)),
    ("a_watched_codes", "watched_codes", ()),
    ("a_get_kv", "get_kv", ("nope",)),
    ("a_save_report", "save_report", ("000001", {"analysis": {}})),
]


@pytest.mark.asyncio
async def test_every_async_wrapper_runs_off_the_event_loop(isolated_db: Path, monkeypatch) -> None:
    """11 个 a_xxx 逐个验，不许有一个漏包 to_thread。

    只抽查几个的话，剩下的封装哪怕忘了包 to_thread 也能蒙混过关——而它们一样会在
    请求路径上阻塞整个 loop。做法是把对应的同步函数逐个换成探针，跑一遍 a_xxx，
    要求同步函数确实被调用、且不在事件循环所在线程。
    """
    _seed_watchlist(1)
    loop_thread = threading.current_thread().name

    for aname, sname, args in _ASYNC_WRAPPER_TABLE:
        seen: list[str] = []
        real = getattr(storage, sname)

        def spy(*a: object, _real: object = real, _seen: list[str] = seen) -> object:
            _seen.append(threading.current_thread().name)
            return _real(*a)  # type: ignore[operator]

        monkeypatch.setattr(storage, sname, spy)
        await getattr(storage, aname)(*args)

        assert seen, f"{aname} 没有真正调用到 {sname}，封装的表对应关系可能已经变了"
        assert seen[0] != loop_thread, f"{aname} 仍在事件循环线程执行，等于没解决问题"
        assert seen[0].startswith("asyncio"), f"{aname} 跑在意外线程：{seen[0]}"


@pytest.mark.asyncio
async def test_async_wrapper_does_not_block_the_event_loop(isolated_db: Path, monkeypatch) -> None:
    """a_xxx 执行期间事件循环必须还能调度别的协程——「不阻塞」的实质在这里。

    光看线程名只能证明「跑在别的线程」，证明不了「那个线程卡住时 loop 不受影响」。
    这里把同步函数换成会卡 0.3 秒的版本，同时放一个 ticker 协程在 loop 上数数：
    真把阻塞挪出去了，ticker 能数到几十次；要是退化成直接同步调用，一次都数不到。
    """
    _seed_watchlist(1)
    real = storage.list_watchlist

    def slow() -> list[dict[str, object]]:
        time.sleep(0.3)
        return real()

    monkeypatch.setattr(storage, "list_watchlist", slow)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    tick_task = asyncio.create_task(ticker())
    rows = await storage.a_list_watchlist()
    tick_task.cancel()
    try:
        await tick_task
    except asyncio.CancelledError:
        pass

    assert len(rows) == 1
    # 0.3s / 0.005s ≈ 60 次。下限取 10：容得下慢机器，又远大于同步调用的 0 次
    assert ticks >= 10, (
        f"a_xxx 执行期间事件循环应能继续调度其他协程，实际只转了 {ticks} 次"
    )


class _CountingLock:
    """统计「同一时刻有几个线程在锁内」的锁代理。

    _lock 是模块级名字，同步函数里 `with _lock:` 每次都从模块全局现取，所以可以整体
    换成一个代理：真锁照加，顺带数并发。数并发必须数在锁内而不是数在函数入口——
    多个线程同时进函数是正常的，真正要求互斥的是 _lock 保护的那段临界区。
    """

    def __init__(self, real: threading.Lock) -> None:
        self._real = real
        self._guard = threading.Lock()
        self.inside = 0
        self.max = 0

    def __enter__(self) -> "_CountingLock":
        self._real.acquire()
        with self._guard:
            self.inside += 1
            self.max = max(self.max, self.inside)
        return self

    def __exit__(self, *_exc: object) -> bool:
        with self._guard:
            self.inside -= 1
        self._real.release()
        return False


@pytest.mark.asyncio
async def test_concurrent_async_wrappers_stay_serialized(isolated_db: Path, monkeypatch) -> None:
    """8 个协程并发打同一个 a_xxx：结果都要对，且 DB 临界区恒为单线程。

    这是 to_thread 方案的核心风险点：_lock 是 threading.Lock（不可重入），一旦有人在
    持锁期间 await、或者临界区里发生重入，就会永久卡死。这里在锁内数并发——
    恒为 1 说明串行化没被多线程破坏；跑完能收敛回 0 说明没有调用卡在锁里出不来。
    """
    _seed_watchlist(5)
    real = storage.list_watchlist

    def slow() -> list[dict[str, object]]:
        time.sleep(0.02)  # 放大竞态窗口，让并发问题有机会暴露
        return real()

    monkeypatch.setattr(storage, "list_watchlist", slow)
    counting = _CountingLock(storage._lock)
    monkeypatch.setattr(storage, "_lock", counting)

    results = await asyncio.gather(*[storage.a_list_watchlist() for _ in range(8)])

    assert all(len(r) == 5 for r in results), "并发读应各自拿到完整结果"
    assert counting.max == 1, f"DB 临界区应被 _lock 串行化，实测最大并发 {counting.max}"
    assert counting.inside == 0, "有调用没退出临界区，说明锁的释放路径有问题"
