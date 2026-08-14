"""带 TTL 的异步内存缓存，附单飞（single-flight）合并，避免刷新风暴打爆数据源。"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    def peek(self, key: str) -> Any | None:
        item = self._data.get(key)
        if not item:
            return None
        expire_at, value = item
        if expire_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = (time.monotonic() + ttl, value)

    def drop(self, key: str) -> None:
        self._data.pop(key, None)

    def drop_prefix(self, prefix: str) -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(key, None)

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get_or_set(
        self,
        key: str,
        ttl: float,
        loader: Callable[[], Awaitable[Any]],
        *,
        force: bool = False,
    ) -> Any:
        if not force:
            cached = self.peek(key)
            if cached is not None:
                return cached
        lock = await self._lock_for(key)
        async with lock:
            # double check：等锁期间可能已被别的协程填充
            if not force:
                cached = self.peek(key)
                if cached is not None:
                    return cached
            value = await loader()
            if value is not None:
                self.put(key, value, ttl)
            return value


cache = TTLCache()
