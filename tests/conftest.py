"""Pytest 全局配置。

DATA_DIR 必须在 backend.config 加载前设置（否则测试会写到真实数据库），
因此用模块顶层代码直接改环境变量，不用 fixture。
backend.config 也在此处显式导入并硬断言隔离生效。

新增主题测试时不需要修改本文件——只要遵守：
  - 测试模块放在 tests/ 下；
  - 不要 import backend 之前已加载；
  - 任何写盘的 fixture 都用 `tmp_path` 而非 DATA_DIR。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 临时数据目录，避免污染工作区 / CI 环境。
# 选 tempfile.mkdtemp 而不是 pytest 的 tmp_path，因为 tmp_path 是 fixture 级别
# （每个测试都新建），而 backend.config.settings.DATA_DIR 在模块加载时就锁死。
_tmp = tempfile.mkdtemp(prefix="board-pytest-")
os.environ["DATA_DIR"] = _tmp

# 把项目根加入 sys.path，确保 `import backend.xxx` 可解析。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 防御：若 backend.config 已被 import 过，DATA_DIR 已固化，必须失败。
# 避免"在 REPL 里先 import backend 再跑 pytest"导致测试写坏真实数据库。
from backend.config import settings  # noqa: E402

if settings.DATA_DIR != Path(_tmp):
    raise RuntimeError(
        f"pytest DATA_DIR 隔离失效：实际={settings.DATA_DIR}，期望={_tmp}。"
        "请以独立进程运行 pytest，不要在已导入 backend 的会话中调用。"
    )


# pytest 配置：让异步 fixture/test 可直接用 asyncio
import pytest  # noqa: E402

def pytest_collection_modifyitems(config, items):
    """自动给 async def test_xxx 加 asyncio 标记（无需 @pytest.mark.asyncio）。"""
    for item in items:
        if item.get_closest_marker("asyncio") is None:
            if isinstance(item, pytest.Function) and item.obj.__code__.co_flags & 0x100:
                # CO_COROUTINE = 0x100, 见 Python inspect.iscoroutinefunction
                item.add_marker(pytest.mark.asyncio)
