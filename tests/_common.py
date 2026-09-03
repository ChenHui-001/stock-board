"""测试公共导入块：集中各测试文件原先逐份复制的样板 import。

`from tests._common import *` 一次性拿到：
  - asyncio / time / Path 等标准库
  - 18 个 backend 子模块 + settings

历史背景：此前 12 个测试文件各自复制同一份 6 行 import 块，
backend 新增模块时要同步改 12 处，漏改即漂移，故收敛到单点。
"""
from __future__ import annotations

import asyncio  # noqa: F401
import time  # noqa: F401
from pathlib import Path  # noqa: F401

from backend import (  # noqa: F401
    analysis, api, cache, check_sources, hotspot, hotspot_ai, hotspot_search,
    indicators, llm, llmcfg, metrics, news, providers, reports, scorecfg,
    service, storage, value_screener, valuecfg,
)
from backend.config import settings  # noqa: F401
