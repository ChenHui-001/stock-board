"""Prometheus 指标：单源抓取请求 / 熔断 / 耗时等可观测性数据。

模块级单例（用 prometheus_client.REGISTRY 共享全局注册表），避免重复创建。
被 hotspot.SourceStat 与 hotspot._fetch_one/_fetch_all 调用，/metrics 端点
（main.py）以 text/plain 暴露给 Prometheus 抓取。

所有指标命名遵循 Prometheus 规范：
- Counter: hotspot_<noun>_total
- Gauge:   hotspot_<noun>
- Histogram: hotspot_<noun>_seconds
"""
from __future__ import annotations

import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# 单源抓取请求计数：labels={source, result}
# result ∈ {success, timeout, circuit_open, error}，success/error 是 HTTP 层面结果，
# timeout 是触发 asyncio.TimeoutError，circuit_open 是熔断短路。
SOURCE_REQUESTS = Counter(
    "hotspot_source_requests_total",
    "热点单源抓取请求计数",
    labelnames=("source", "result"),
)

# 当前各源连续失败次数（与 SourceStat._consecutive_failures 同步）
SOURCE_FAILURES = Gauge(
    "hotspot_source_consecutive_failures",
    "各源当前连续失败次数（达到 open_at 即触发熔断）",
    labelnames=("source",),
)

# 各源是否处于熔断中（0/1）
SOURCE_CIRCUIT_OPEN = Gauge(
    "hotspot_source_circuit_open",
    "各源熔断状态：1=冷却中，0=正常",
    labelnames=("source",),
)

# 各源累计返回条目数
SOURCE_ITEMS = Counter(
    "hotspot_source_items_total",
    "各源累计成功返回的快讯条目数",
    labelnames=("source",),
)

# 单源抓取耗时直方图（秒）：分桶覆盖快源（<0.5s）到慢源（10s）。
# buckets 与 _TIMEOUT_BY_TIER 对齐，便于按 tier 看 p95/p99。
SOURCE_DURATION = Histogram(
    "hotspot_source_fetch_duration_seconds",
    "单源抓取耗时（含重试，单位：秒）",
    labelnames=("source", "result"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 6.0, 8.0, 10.0),
)


def observe_duration(source: str, result: str, seconds: float) -> None:
    """单源抓取耗时打点（独立暴露，便于 _fetch_one 内部一次调用打完所有点）。"""
    SOURCE_DURATION.labels(source=source, result=result).observe(seconds)


def update_source_gauge(source: str, consecutive_failures: int, circuit_open: bool) -> None:
    """同步 SourceStat 状态到 Gauge：连续失败次数 + 熔断 0/1。"""
    SOURCE_FAILURES.labels(source=source).set(consecutive_failures)
    SOURCE_CIRCUIT_OPEN.labels(source=source).set(1 if circuit_open else 0)


def export() -> tuple[bytes, str]:
    """生成 Prometheus 文本格式指标输出（供 /metrics 端点返回）。"""
    return generate_latest(), CONTENT_TYPE_LATEST


# 模块级时间戳基准：避免 _fetch_one 内部 `import time` 重复导入
def now_ts() -> float:
    """perf_counter 单调时钟：用于计算单源耗时。"""
    return time.perf_counter()