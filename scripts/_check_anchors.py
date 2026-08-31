"""校验 scripts/_trim_api.py 里使用的 marker 是否都能在 api.py 中唯一匹配。"""
from pathlib import Path

API = Path("backend/api.py")
text = API.read_text(encoding="utf-8")

ANCHORS = [
    "def _fail(exc: Exception, hint: str) -> HTTPException:",
    "# ------------------------------------------------------------------ AI 并发去重",
    "# ------------------------------------------------------------------ 数据源健康自检",
    "def _mark_value_watched(result: dict[str, Any]) -> None:",
    '@router.get("/value/weights")',
    "def _ai_summary_from_report(report: dict[str, Any]) -> dict[str, Any]:",
    "async def _generate_rule_summary(code: str) -> dict[str, Any]:",
]

for needle in ANCHORS:
    matches = [i for i, ln in enumerate(text.splitlines(), 1) if ln.strip() == needle.strip()]
    status = "OK" if len(matches) == 1 else "BAD"
    print(f"[{status}] {needle!r} -> 行号 {matches}")
