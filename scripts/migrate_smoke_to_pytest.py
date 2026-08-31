"""把 _smoke_test_legacy.py 的 test_X 函数批量转换成 pytest 格式。

转换规则：
  check(name, cond)           → assert cond
  check(name, cond, detail)   → assert cond, detail

name 是测试运行器里给人看的分类标签；pytest 已经按函数名/文件分组了，
detail 多为 f"got=..." 等可执行字符串表达式，原样保留作为失败信息。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LEGACY = Path("backend/_smoke_test_legacy.py")
OUT_DIR = Path("tests")
OUT_DIR.mkdir(exist_ok=True)


def extract_test_blocks(src: str) -> list[tuple[str, str]]:
    """抓出每个 `def test_X(...):` 顶层的整段代码。"""
    out: list[tuple[str, str]] = []
    lines = src.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("def test_"):
            name = line.split("(", 1)[0].split("def ", 1)[1].strip()
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.startswith("def ") or nxt.startswith("class ") or (
                    nxt.strip() and not (nxt.startswith(" ") or nxt.startswith("\t"))
                ):
                    break
                j += 1
            body = "".join(lines[i:j])
            out.append((name, body))
            i = j
        else:
            i += 1
    return out


TOPIC_MAP: dict[str, str] = {
    "test_describe_exc": "test_utils.py",
    "test_items_fingerprint": "test_utils.py",
    "test_safe_url": "test_utils.py",
    "test_json_repair": "test_llm.py",
    "test_llm_timeout_floor": "test_llm.py",
    "test_fingerprint": "test_llm.py",
    "test_llm_profiles": "test_llm.py",
    "test_llm_failover": "test_llm.py",
    "test_cache": "test_cache.py",
    "test_ai_lock": "test_cache.py",
    "test_ai_cache_freshness": "test_cache.py",
    "test_ai_cache_blank_degraded_invalidated": "test_cache.py",
    "test_value_screener": "test_value_screener.py",
    "test_value_screen_e2e": "test_value_screener.py",
    "test_value_weights": "test_value_screener.py",
    "test_model_filter": "test_providers.py",
    "test_quote_racing": "test_providers.py",
    "test_registry": "test_providers.py",
    "test_news_interpret": "test_news.py",
    "test_reports_interpret": "test_reports.py",
    "test_financials": "test_financials.py",
    "test_hotspot": "test_hotspot.py",
    "test_hotspot_ai": "test_hotspot.py",
    "test_rule_precision": "test_analysis.py",
    "test_ai_sanitize": "test_analysis.py",
    "test_payload_quality": "test_analysis.py",
    "test_kline_stale": "test_indicators.py",
    "test_indicators": "test_indicators.py",
    "test_watch_monitor": "test_indicators.py",
    "test_backtest_selftest": "test_helpers.py",
    "test_check_sources_backtest_struct": "test_helpers.py",
}

HEADER = '"""{title}。"""\nfrom __future__ import annotations\n\n'


def main() -> int:
    src = LEGACY.read_text(encoding="utf-8")
    blocks = dict(extract_test_blocks(src))

    buckets: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    for name, body in blocks.items():
        if name not in TOPIC_MAP:
            print(f"[skip] unmapped: {name}")
            continue
        target = TOPIC_MAP[name]
        title = target.removesuffix(".py").replace("test_", "").replace("_", " ").title()
        titles[target] = title
        buckets.setdefault(target, []).append(body)

    for target, bodies in buckets.items():
        path = OUT_DIR / target
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        # 单行 / 多行 check(...) 统一交给 _fix_remaining_check.transform 用 tokenize 处理
        # （正则处理多行会被 \s* 吃掉换行导致 cond 截断）
        from _fix_remaining_check import transform as _t
        new_block = "\n\n\n".join(_t(b) for b in bodies)
        if existing:
            path.write_text(existing + "\n\n" + new_block, encoding="utf-8")
        else:
            path.write_text(HEADER.format(title=titles[target]) + new_block, encoding="utf-8")
        print(f"[ok] {target}: +{len(bodies)} tests")

    return 0


if __name__ == "__main__":
    sys.exit(main())
