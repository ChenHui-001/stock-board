"""阶段 3 收尾：从 api.py 删除已迁出到 api_deps 的 helper 定义。

删除 4 段（按文件中出现的先后顺序）：
  1. _fail + _sentiment_stats
  2. AI 并发去重（_AILock/_ai_locks/_with_ai_lock）+ AI 缓存校验（_REQUIRED_REPORT_FIELDS/_BLANK_LLM_REASON_RE/REPORT_SCHEMA_VERSION/_cache_fresh/_cached_report/_cached_brief_report）
  3. _mark_value_watched
  4. _ai_summary_from_report

每段用唯一"锚行"定位起点，"下一个顶层定义"定位终点；锚点用原文（含中文），不靠行号。
"""
from __future__ import annotations

import re
from pathlib import Path

API = Path("backend/api.py")
src = API.read_text(encoding="utf-8")
orig_len = src.count("\n")

# 顶层分隔哨兵：以下 marker 在文件中各只出现一次，且不在字符串内（结构稳定）
# 删段时按 marker 切成 [start, end)。
# 用作"删除段起点"的 marker 应当是段之前最后一个非空行；"删除段终点"marker 应当紧跟段的下一行（即保留 marker 前的空白）。
ANCHORS = [
    # 段 1: _fail + _sentiment_stats  (line ~66-81, blank ~82-83)
    {
        "name": "fail+sentiment_stats",
        "start_marker": "def _fail(exc: Exception, hint: str) -> HTTPException:",
        "end_keep_until": "# ------------------------------------------------------------------ AI 并发去重",
    },
    # 段 2: AI 锁 + AI 缓存校验  (从段标题到 _cached_brief_report 结束的空白行)
    {
        "name": "ai_lock_and_cache_validation",
        "start_marker": "# ------------------------------------------------------------------ AI 并发去重",
        "end_keep_until": "# ------------------------------------------------------------------ 数据源健康自检",
    },
    # 段 3: _mark_value_watched
    {
        "name": "_mark_value_watched",
        "start_marker": "def _mark_value_watched(result: dict[str, Any]) -> None:",
        "end_keep_until": "@router.get(\"/value/weights\")",
    },
    # 段 4: _ai_summary_from_report
    {
        "name": "_ai_summary_from_report",
        "start_marker": "def _ai_summary_from_report(report: dict[str, Any]) -> dict[str, Any]:",
        "end_keep_until": "async def _generate_rule_summary(code: str) -> dict[str, Any]:",
    },
]


def find_line(text: str, needle: str) -> int:
    """needle 必须完整匹配一行（去除前导空格后）。返回 1-based 行号。"""
    for i, ln in enumerate(text.splitlines(), 1):
        if ln.strip() == needle.strip():
            return i
    raise SystemExit(f"未找到锚点：{needle!r}")


def delete_section(text: str, start_marker: str, end_marker: str) -> str:
    lines = text.splitlines(keepends=False)
    start = next(i for i, ln in enumerate(lines) if ln.strip() == start_marker.strip())
    end = next(i for i, ln in enumerate(lines) if ln.strip() == end_marker.strip())
    assert start < end, f"start {start} 应当小于 end {end}（marker={start_marker!r}）"
    # 保留 end_marker 前的空行：end_marker 通常是下一个 section 的"标题"行；其上方
    # 通常已有 1-2 个空行分隔，删段后保留 2 个空行作为 section 分隔
    # 我们把 [start, end) 整段删除（end_marker 不删），并把空行数校准到 ≤2
    del lines[start:end]
    # 把 start 之前的两空行规范成 2 个
    # 找 start 处往前的连续空行
    k = start
    while k > 0 and lines[k - 1] == "":
        k -= 1
    # k 是第一个非空行；start-k 是开头的空行数；我们想保留 2 个空行
    blanks_before = start - k
    if blanks_before > 2:
        # 删除多余空行
        del lines[k + 2:start]
    elif blanks_before < 2:
        # 补足到 2 个空行
        lines[k:k] = [""] * (2 - blanks_before)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


new = src
for a in ANCHORS:
    new = delete_section(new, a["start_marker"], a["end_keep_until"])

API.write_text(new, encoding="utf-8")
new_len = new.count("\n")
print(f"原始行数: {orig_len}, 修改后行数: {new_len}, 减少: {orig_len - new_len}")
