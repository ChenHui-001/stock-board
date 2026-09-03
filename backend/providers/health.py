"""数据源健康统计与熔断（从 providers/__init__.py 拆出）。

- ProviderStats：滑动窗口内的成功率 / 平均延迟 / 综合评分。
- HealthMixin：熔断状态（连续失败 N 次冷却 M 秒）与按能力筛选可用源。
  Registry 与本 Mixin 共享 _fail / _blocked_until / _stats 三个实例属性
  （由 Registry.__init__ 创建）。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .base import Provider, limiter

if TYPE_CHECKING:  # 仅类型标注用，避免运行时循环导入
    from . import Registry

# 与拆分前保持同一 logger 名，日志输出行为完全一致
log = logging.getLogger("providers.registry")

# 熔断：连续失败 N 次后冷却 M 秒
BREAK_AFTER = 3
COOLDOWN = 60.0


@dataclass
class ProviderStats:
    """单个数据源的运行时质量统计（滑动窗口）。"""

    name: str
    ok: int = 0
    fail: int = 0
    total_ms: int = 0          # 成功请求总耗时（毫秒）
    last_ok_at: float = 0.0    # 最后一次成功时间戳
    last_quote_time: str = ""  # 最近一次行情时间
    _latencies: deque[int] = field(default_factory=lambda: deque(maxlen=20))

    def record(self, ok: bool, latency_ms: int, quote_time: str = "") -> None:
        if ok:
            self.ok += 1
            self.total_ms += latency_ms
            self._latencies.append(latency_ms)
            self.last_ok_at = time.monotonic()
            if quote_time:
                self.last_quote_time = quote_time
        else:
            self.fail += 1

    @property
    def requests(self) -> int:
        return self.ok + self.fail

    @property
    def success_rate(self) -> float:
        total = self.requests
        return self.ok / total if total else 1.0

    @property
    def avg_latency_ms(self) -> int:
        if not self._latencies:
            return 0
        return int(sum(self._latencies) / len(self._latencies))

    @property
    def score(self) -> float:
        """综合评分：成功率 70% + 延迟 30%。范围 0~1，越高越好。"""
        if self.requests == 0:
            return 0.5  # 无样本时给中等分，不贸然优先
        # 延迟分：假设 0ms=1.0，2000ms=0.0
        latency_score = max(0.0, 1.0 - self.avg_latency_ms / 2000)
        return round(self.success_rate * 0.7 + latency_score * 0.3, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "fail": self.fail,
            "success_rate": round(self.success_rate, 2),
            "avg_latency_ms": self.avg_latency_ms,
            "score": self.score,
            "last_ok_at": self.last_ok_at,
            "last_quote_time": self.last_quote_time,
        }


class HealthMixin:
    """熔断与健康度（依赖宿主的 providers/_fail/_blocked_until/_stats 属性）。"""

    def _stat(self: "Registry", name: str) -> ProviderStats:
        return self._stats.setdefault(name, ProviderStats(name=name))

    # ------------------------------------------------------------ 健康度
    def _available(self: "Registry", cap: str) -> list[Provider]:
        now = time.monotonic()
        ready = [
            p for p in self.providers
            if cap in p.caps and self._blocked_until.get(p.name, 0) <= now
        ]
        if not ready:
            # 全部处于熔断中：放行以免整站不可用
            ready = [p for p in self.providers if cap in p.caps]
        # 资讯、研报和财报按用户指定的来源优先级独立调度：同花顺主，
        # 东方财富辅；不改变行情/资金等其他能力的 PROVIDER_ORDER。
        if cap in {"news", "reports", "financials"}:
            priority = {"ths": 0, "eastmoney": 1}
            ready.sort(key=lambda p: priority.get(p.name, 2))
            return ready
        # 行情按综合评分动态排序：快且稳的源优先，减少固定顺序下
        # 东财频控对首屏的阻塞。
        if cap == "quotes":
            ready.sort(key=lambda p: self._stat(p.name).score, reverse=True)
        return ready

    def _mark_ok(self: "Registry", name: str) -> None:
        self._fail.pop(name, None)
        self._blocked_until.pop(name, None)

    def _mark_fail(self: "Registry", name: str) -> None:
        count = self._fail.get(name, 0) + 1
        self._fail[name] = count
        if count >= BREAK_AFTER:
            self._blocked_until[name] = time.monotonic() + COOLDOWN
            log.warning("数据源 %s 连续失败 %d 次，冷却 %.0fs", name, count, COOLDOWN)

    def health(self: "Registry") -> list[dict[str, Any]]:
        now = time.monotonic()
        result: list[dict[str, Any]] = []
        for p in self.providers:
            stat = self._stat(p.name)
            result.append({
                "name": p.name,
                "caps": sorted(p.caps),
                "fails": self._fail.get(p.name, 0),
                "cooling": max(0, round(self._blocked_until.get(p.name, 0) - now)),
                "ok": stat.ok,
                "fail": stat.fail,
                "success_rate": round(stat.success_rate, 2),
                "avg_latency_ms": stat.avg_latency_ms,
                "score": stat.score,
                "last_quote_time": stat.last_quote_time,
            })
        return result

    @staticmethod
    def throttled_hosts() -> dict[str, float]:
        return limiter.status()

    def host_stats(self: "Registry") -> dict[str, dict[str, Any]]:
        return limiter.host_stats()
