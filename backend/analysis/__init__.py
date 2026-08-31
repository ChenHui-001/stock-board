"""AI 分析入口 + 子模块重导出。

模块边界：
  - `prompts`     投喂 LLM 的系统提示词与详情数据压缩
  - `rule_engine` 无 LLM 时的确定性决策与回退路径
  - `sanitize`    LLM 输出校验与 LLM/规则分歧检测

`from backend import analysis` / `from . import analysis` 仍是子包；
向后兼容原 `analysis.analyze / analysis.rule_based / analysis.build_payload` 调用。
"""
from __future__ import annotations

import json as _json
import logging
from typing import Any

from .. import llm
from ..utils import now
from .prompts import (
    ACTIONS,
    FEWSHOT_EXAMPLE,
    SYSTEM_PROMPT,
    build_payload,
    system_prompt,
)
from .rule_engine import (
    _annotate_intraday,
    _confidence_reason_text,
    _fundamental_score,
    _intraday_score,
    _news_score,
    _reports_score,
    _volume_confirm,
    rule_based,
)
from .sanitize import _diff_divergence, _sanitize

log = logging.getLogger("analysis")

__all__ = [
    # 公共 API
    "ACTIONS",
    "SYSTEM_PROMPT",
    "FEWSHOT_EXAMPLE",
    "system_prompt",
    "rule_based",
    "build_payload",
    "analyze",
    # 私有符号：smoke_test 与 pytest 用得到，外部代码请走显式子模块路径
    "_annotate_intraday",
    "_confidence_reason_text",
    "_fundamental_score",
    "_intraday_score",
    "_news_score",
    "_reports_score",
    "_volume_confirm",
    "_sanitize",
    "_diff_divergence",
]


async def analyze(
    detail: dict[str, Any],
    news: list[dict[str, Any]] | None = None,
    reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """news/reports 为资讯与券商研报，财报从 detail.financials 读取并一并投喂。

    LLM 路径投喂全文，规则引擎按财报同比与质量指标做保守基本面评分。
    """
    financials = detail.get("financials") or {}
    fallback = rule_based(detail, news, reports, financials)
    price = detail["quote"].get("price")
    meta: dict[str, Any] = {
        "engine": "rule",
        "model": "内置规则引擎",
        "generated_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        "degraded_reason": "",
        "news_count": len(news or []),
        "reports_count": len(reports or []),
        "financials_count": len(financials.get("rows") or []),
    }

    if not llm.available():
        meta["degraded_reason"] = "未配置 LLM_API_KEY，使用内置规则引擎"
        meta["divergence"] = {"status": "rule_only"}
        return {"analysis": fallback, "meta": meta, "input": build_payload(detail, news, reports)}

    payload = build_payload(detail, news, reports)
    user = (
        "请分析下面这只 A 股的持仓决策，严格按系统提示的 JSON 结构输出：\n\n"
        + _json.dumps(payload, ensure_ascii=False, indent=1)
    )
    try:
        raw, llm_meta = await llm.chat_json(system_prompt(), user)
        analysis = _sanitize(raw, fallback, price)
        meta.update(
            engine="llm",
            model=llm_meta.get("model"),
            usage=llm_meta.get("usage"),
            # LLM 路径：与规则引擎结论对比，给用户知情权
            divergence=_diff_divergence(analysis, fallback),
        )
        return {"analysis": analysis, "meta": meta, "input": payload}
    except llm.LLMError as exc:
        log.warning("LLM 分析失败，降级规则引擎：%s", exc)
        meta["degraded_reason"] = f"AI 服务调用失败（{exc}），已降级为内置规则引擎"
        meta["divergence"] = {"status": "degraded"}  # LLM 失败，分歧无意义
        return {"analysis": fallback, "meta": meta, "input": payload}
