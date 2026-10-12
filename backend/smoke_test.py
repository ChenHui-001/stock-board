"""Smoke-test shim：委托给 pytest。

历史：本文件曾是 184KB 的内联测试套件（按顺序调用 30 个 test_xxx 函数）。
v3 重构后保留本入口，让 `python -m backend.smoke_test` 与
`python backend/smoke_test.py` 继续可用（auto-commit-watch.ps1 与 CI
仍按这条路径调用）。

所有用例已迁移到 tests/，按主题分文件：
  - tests/test_utils.py           通用工具函数
  - tests/test_llm.py             LLM JSON 修复 / 配置指纹 / 多档案 / 故障转移
  - tests/test_cache.py           TTL 缓存 + 单飞 + AI 锁 + AI 缓存作废
  - tests/test_providers.py       数据源注册表 / 行情竞速 / 模型过滤
  - tests/test_helpers.py         自检 / 回测脚本结构 / 评分辅助
  - tests/test_hotspot.py         热点聚合 + AI 关联分析
  - tests/test_news.py            个股资讯解读
  - tests/test_reports.py         券商研报解读
  - tests/test_financials.py      财报拉取与口径处理
  - tests/test_analysis.py        规则引擎精度 + 校验 + payload 质量
  - tests/test_indicators.py      MA/支撑压力/资金/两融/watch_monitor
  - tests/test_value_screener.py  价值投资选股 + 权重 + 端到端

新增测试：直接放到对应主题文件，不要再加回本 shim。
"""
from __future__ import annotations

import sys

import pytest


def main(argv: list[str] | None = None) -> int:
    """运行测试套件并返回退出码。

    退出码：
      - 0  全部通过
      - 1  有失败
      - 5  收集到 0 个用例（tests/ 目录缺失/为空/路径错）——视为失败
      - 其他  pytest 自身错误

    历史：v3 重构初期 tests/ 迁移未完，曾把退出码 5 短路成 0（CI 假阳性：
    目录被误删时 CI 全绿但零测试）。现迁移已完成，收集数为 0 必须失败。
    """
    args = list(argv) if argv else ["tests"]
    code = pytest.main(args)
    if code == 5:
        print(
            "smoke_test: FATAL - pytest 未收集到任何用例（tests/ 目录缺失、为空或路径错误）",
            file=sys.stderr,
        )
        return 5
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
