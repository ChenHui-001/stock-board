"""带 TTL 的异步内存缓存，附单飞（single-flight）合并，避免刷新风暴打爆数据源。"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

# 过期项若只在「再次读取同一个 key」时被动清除，遇到 key 基数无上界的场景
# （如 search:<用户输入>）就会只增不减。这里加两道兜底：定期清扫 + 条目数硬上限。
SWEEP_INTERVAL = 60.0
MAX_ENTRIES = 4096

# 负缓存：loader 返回 None（源端暂无数据）时用短 TTL 缓存一个哨兵，
# 避免同 key 高频请求反复穿透到上游（缓存穿透）。外部 peek 对哨兵不可见。
NEG_TTL = 30.0
_NEG = object()


class _KeyLock:
    """带等待计数的锁：计数归零即可从锁表摘除，避免锁表随 key 基数无限膨胀。"""

    __slots__ = ("lock", "waiters")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.waiters = 0


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, _KeyLock] = {}
        self._next_sweep = 0.0

    # ------------------------------------------------------------ 读写
    def _peek_raw(self, key: str) -> Any | None:
        """原始读取：命中（含负缓存哨兵）返回存储值，未命中/过期返回 None。"""
        item = self._data.get(key)
        if not item:
            return None
        expire_at, value = item
        if expire_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def peek(self, key: str) -> Any | None:
        value = self._peek_raw(key)
        return None if value is _NEG else value

    def put(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = (time.monotonic() + ttl, value)
        self._maybe_sweep()

    def drop(self, key: str) -> None:
        self._data.pop(key, None)

    def drop_prefix(self, prefix: str) -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(key, None)

    def clear(self) -> None:
        """清空全部条目与单飞锁。生产代码不应调用（缓存复用可避免上游重读），
        仅供测试 setup/teardown 强制重建上下文。"""
        self._data.clear()
        self._locks.clear()

    def stats(self) -> dict[str, int]:
        """条目数与在用锁数，用于诊断内存占用。"""
        return {"entries": len(self._data), "locks": len(self._locks)}

    # ------------------------------------------------------------ 清扫
    def _maybe_sweep(self) -> None:
        """清掉已过期条目；仍超上限则按过期时间由早到晚淘汰。

        全程无 await，事件循环单线程下不会与其他协程交错，无需加锁。
        """
        now = time.monotonic()
        if now < self._next_sweep and len(self._data) <= MAX_ENTRIES:
            return
        self._next_sweep = now + SWEEP_INTERVAL
        for key in [k for k, (expire_at, _) in self._data.items() if expire_at < now]:
            self._data.pop(key, None)
        if len(self._data) > MAX_ENTRIES:
            # stale: 兜底数据 TTL 最长（24h），排在最后，被淘汰的优先是短 TTL 的行情/搜索
            ordered = sorted(self._data.items(), key=lambda kv: kv[1][0])
            for key, _ in ordered[: len(self._data) - MAX_ENTRIES]:
                self._data.pop(key, None)

    # ------------------------------------------------------------ 单飞
    # 取/放锁全程无 await，单线程事件循环下是原子的，不需要额外的 guard 锁。
    def _acquire(self, key: str) -> _KeyLock:
        entry = self._locks.get(key)
        if entry is None:
            entry = _KeyLock()
            self._locks[key] = entry
        entry.waiters += 1
        return entry

    def _release(self, key: str, entry: _KeyLock) -> None:
        entry.waiters -= 1
        if entry.waiters <= 0 and self._locks.get(key) is entry:
            self._locks.pop(key, None)

    async def get_or_set(
        self,
        key: str,
        ttl: float,
        loader: Callable[[], Awaitable[Any]],
        *,
        force: bool = False,
        negative_ttl: float = NEG_TTL,
    ) -> Any:
        if not force:
            cached = self._peek_raw(key)
            if cached is not None:
                return None if cached is _NEG else cached
            # miss 路径顺带触发清扫：若只在 put() 里清扫，纯读场景下过期项
            # 会一直滞留。_maybe_sweep 自带 60s 间隔护栏，高频调用无额外开销。
            self._maybe_sweep()
        entry = self._acquire(key)
        try:
            async with entry.lock:
                # double check：等锁期间可能已被别的协程填充
                if not force:
                    cached = self._peek_raw(key)
                    if cached is not None:
                        return None if cached is _NEG else cached
                value = await loader()
                if value is not None:
                    self.put(key, value, ttl)
                elif negative_ttl > 0:
                    # 负缓存：源端暂无数据也短暂占位，防同 key 请求穿透上游
                    self.put(key, _NEG, negative_ttl)
                return value
        finally:
            self._release(key, entry)


cache = TTLCache()
