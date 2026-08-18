"""运行时 AI 评分权重：数据库覆盖 + 环境变量兜底，支持界面调整三维分面权重。

优先级：界面保存的配置（kv 表） > 环境变量（SCORE_WEIGHT_TECH/CAPITAL/NEWS）。
权重是三维分面评分的乘数（默认 1.0，范围 0.2~3.0）：调大某面让该维度信号更强。
"""
from __future__ import annotations

import json
from typing import Any

from . import storage
from .config import settings

FIELDS = ("tech", "capital", "news")
# 权重合法范围：过低会抹掉该面信号，过高会盖过其他面
_MIN, _MAX = 0.2, 3.0


def _clamp(value: Any, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return max(_MIN, min(_MAX, f))


def get_weights() -> dict[str, float]:
    """当前生效权重：DB 覆盖优先，未存过的用环境变量兜底。"""
    w: dict[str, float] = {
        "tech": settings.SCORE_WEIGHT_TECH,
        "capital": settings.SCORE_WEIGHT_CAPITAL,
        "news": settings.SCORE_WEIGHT_NEWS,
    }
    raw = storage.get_kv("score_weights")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for k in FIELDS:
                    if k in data:
                        w[k] = _clamp(data[k], w[k])
        except json.JSONDecodeError:
            pass
    return {k: round(w[k], 2) for k in FIELDS}


def save_weights(cfg: dict[str, Any]) -> dict[str, float]:
    """保存权重（clamp 到合法范围），返回保存后的实际值。"""
    current = get_weights()
    clean = {k: _clamp(cfg.get(k), current[k]) for k in FIELDS}
    storage.set_kv("score_weights", json.dumps(clean))
    return {k: round(clean[k], 2) for k in FIELDS}


def reset_weights() -> dict[str, float]:
    """清除界面配置，回退到环境变量。"""
    storage.delete_kv("score_weights")
    return get_weights()


def fingerprint() -> str:
    """当前生效权重的指纹，用于 AI 当日缓存校验（权重变化后旧缓存作废）。"""
    w = get_weights()
    return "|".join(f"{k}:{w[k]:.2f}" for k in FIELDS)
