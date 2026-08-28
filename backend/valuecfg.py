"""运行时价值选股权重：数据库覆盖 + 默认兜底，支持界面调整各评分维度权重。

优先级：界面保存的配置（kv 表 value_weights） > 默认 1.0。
权重表达「相对看重程度」（相对权重，范围 0.2~3.0，默认 1.0）：
综合评分 = BASE_TOTAL × Σ(维度分×权重) / Σ(维度满分×权重)。
默认全 1.0 时即原始分之和（与原行为一致）；调大某维度权重，总分向该
维度强度（分/满分）靠拢——该维度强的股票总分上升、弱的下降，只改变
相对排序而非单方向加分。总分恒在 0~BASE_TOTAL，不因权重放大截断失真。

维度满分（策略口径）：finance=基本面 50、board=板块 10、flow=资金 12、
volume=量价筹码 8、emotion=情绪妖股 12、relative=相对板块强度 8、
position=20日价格位置 6，BASE_TOTAL=106。
"""
from __future__ import annotations

import json
from typing import Any

from . import storage

FIELDS = ("finance", "board", "flow", "volume", "emotion")
# 各维度满分（评分引擎口径，用于归一化权重与前端展示）
DIM_MAXES: dict[str, float] = {"finance": 50.0, "board": 10.0, "flow": 12.0,
                               "volume": 8.0, "emotion": 12.0}
BASE_TOTAL = round(sum(DIM_MAXES.values()), 1)  # 92.0
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
