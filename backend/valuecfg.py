"""运行时价值选股权重：数据库覆盖 + 环境变量兜底，支持界面调整各评分维度权重。

优先级：界面保存的配置（kv 表 value_weights） > 环境变量（VALUE_WEIGHT_*）。
权重是各维度评分的乘数（默认 1.0，范围 0.2~3.0）：调大某维度让该维度信号更强。

维度与策略权重对应（默认全 1.0 即当前行为）：
finance=基本面（默认满分 50）、board=板块（10）、flow=资金（12）、
volume=量价筹码（8）、emotion=情绪妖股（12）。
"""
from __future__ import annotations

import json
from typing import Any

from . import storage

FIELDS = ("finance", "board", "flow", "volume", "emotion")
# 权重合法范围：过低会抹掉该维度信号，过高会盖过其他维度
_MIN, _MAX = 0.2, 3.0
_KEY = "value_weights"


def _clamp(value: Any, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return max(_MIN, min(_MAX, f))


def get_weights() -> dict[str, float]:
    """当前生效权重：DB 覆盖优先，未存过的用默认 1.0。"""
    w: dict[str, float] = {k: 1.0 for k in FIELDS}
    raw = storage.get_kv(_KEY)
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
    storage.set_kv(_KEY, json.dumps(clean))
    return {k: round(clean[k], 2) for k in FIELDS}


def reset_weights() -> dict[str, float]:
    """清除界面配置，回退到默认 1.0。"""
    storage.delete_kv(_KEY)
    return get_weights()


def fingerprint() -> str:
    """当前生效权重的指纹：权重变化后旧选股缓存作废。"""
    w = get_weights()
    return "|".join(f"{k}:{w[k]:.2f}" for k in FIELDS)
