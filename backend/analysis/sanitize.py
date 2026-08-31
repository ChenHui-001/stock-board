"""LLM 输出校验与 LLM/规则分歧检测。

sanitize = 两道防线：
  1. `_sanitize`：把 LLM 返回的 dict 强行按需求 6.3 的硬约束对齐，
     不合规字段用 `rule_based` 的结果兜底，并保留可读 `action_note` 解释。
  2. `_diff_divergence`：LLM 路径结束后与规则引擎结论对比，
     给前端 ⚠ 标记让用户知情。

为什么单独成模块：
  校验逻辑在原 analysis.py 里夹在 LLM 调用前后，与 prompt/rule 是不同关注点；
  单独抽出后 sanitize 可以被单元测试覆盖，无需 mock LLM。
"""
from __future__ import annotations

from typing import Any

from .rule_engine import ACTIONS


def _sanitize(result: dict[str, Any], fallback: dict[str, Any], price: float | None) -> dict[str, Any]:
    """确保 LLM 输出满足需求 6.3 的硬约束，缺失项用规则引擎结果补齐。"""
    out: dict[str, Any] = {}
    for section in ("trend", "capital", "fundamental", "risk", "advice"):
        base = dict(fallback.get(section) or {})
        got = result.get(section)
        if isinstance(got, dict):
            for k, v in got.items():
                if v not in (None, "", [], {}):
                    base[k] = v
        out[section] = base

    advice = out["advice"]
    action = str(advice.get("action") or "").strip()
    if action not in ACTIONS:
        matched = next((a for a in ACTIONS if a[:2] in action), None)
        advice["action"] = matched or fallback["advice"]["action"]
        if not matched:
            advice["action_note"] = f"模型返回的建议「{action}」不在规定选项内，已回退规则引擎结论"

    # 价位合理性：偏离现价 50% 以上判为幻觉，回退规则值
    if price:
        for key in ("support", "resistance", "stop_loss", "take_profit"):
            v = advice.get(key)
            try:
                fv = float(v)
            except (TypeError, ValueError):
                advice[key] = fallback["advice"].get(key)
                continue
            if fv <= 0 or not (price * 0.5 <= fv <= price * 1.5):
                advice[key] = fallback["advice"].get(key)
            else:
                advice[key] = round(fv, 2)

    try:
        advice["confidence"] = max(0, min(100, int(float(advice.get("confidence", 70)))))
    except (TypeError, ValueError):
        advice["confidence"] = 70

    # 低置信度撤销激进建议：模型自己都不确定（confidence < 50）却给出
    # 「积极持仓/加仓」或「清仓离场」这类不可逆操作，是典型幻觉/过度自信，
    # 撤销为「持有观望」并标注，避免用户依据低置信结论做出激进操作。
    if advice["confidence"] < 50 and advice["action"] in (ACTIONS[0], ACTIONS[3]):
        advice["action_note"] = (
            f"模型置信度过低（{advice['confidence']}%），已撤销激进建议"
            f"「{advice['action']}」，改为持有观望"
        )
        advice["action"] = ACTIONS[1]

    risk = out["risk"]
    for key in ("opportunities", "risks"):
        val = risk.get(key)
        if isinstance(val, str):
            risk[key] = [val]
        elif not isinstance(val, list) or not val:
            risk[key] = fallback["risk"][key]
        else:
            # 含具体数字（依据）的条目优先，模型泛泛而谈的空话排后，再截断到 5 条
            risk[key] = sorted(
                [str(x) for x in val],
                key=lambda s: (0 if any(c.isdigit() for c in s) else 1),
            )[:5]
    return out


def _diff_divergence(
    llm_result: dict[str, Any],
    rule_fallback: dict[str, Any],
) -> dict[str, Any]:
    """对比 LLM 路径与规则引擎结论，给出分歧标记供前端展示。

    主要比较 4 个维度：
    - action：四选一行动建议
    - direction：tech/capital/news 三面信号方向
    - 总分差距 |LLM.total - rule.total|
    - 置信度差距 |LLM.confidence - rule.confidence|

    仅当 LLM 与规则存在显著分歧时返回 status='conflict'，否则 status='aligned'；
    前端可在 ⚠ 提示中展示具体分歧维度，给用户知情权。
    """
    llm_advice = (llm_result.get("advice") or {})
    rule_advice = (rule_fallback.get("advice") or {})
    llm_scores = (llm_advice.get("scores") or {})
    rule_scores = (rule_advice.get("scores") or {})

    diffs: list[str] = []
    if llm_advice.get("action") != rule_advice.get("action"):
        diffs.append(
            "行动分歧：LLM=" + str(llm_advice.get("action")) + " / 规则=" + str(rule_advice.get("action"))
        )

    dir_diffs: list[str] = []
    for dim in ("tech", "capital", "news"):
        lv = llm_scores.get(dim)
        rv = rule_scores.get(dim)
        try:
            if lv is not None and rv is not None:
                ls = 1 if lv > 0 else (-1 if lv < 0 else 0)
                rs = 1 if rv > 0 else (-1 if rv < 0 else 0)
                if ls != 0 and rs != 0 and ls != rs:
                    label = {"tech": "技术", "capital": "资金", "news": "消息"}[dim]
                    dir_diffs.append(label)
        except TypeError:
            continue
    if dir_diffs:
        diffs.append("信号方向分歧：" + "、".join(dir_diffs))

    score_gap = abs((llm_scores.get("total") or 0) - (rule_scores.get("total") or 0))
    conf_gap = abs((llm_advice.get("confidence") or 0) - (rule_advice.get("confidence") or 0))
    if score_gap >= 20:
        diffs.append("总分差距 %.1f 分" % score_gap)
    if conf_gap >= 15:
        diffs.append("置信度差距 %d%%" % conf_gap)

    return {
        "status": "conflict" if diffs else "aligned",
        "diffs": diffs,
        "score_gap": round(score_gap, 1),
        "conf_gap": conf_gap,
        "llm_action": llm_advice.get("action"),
        "rule_action": rule_advice.get("action"),
    }
