"""权重端点 schema 回归：POST 保存/reset 返回必须通过 response_model 校验。

背景：P2 #18 全量 response_model 上线时，ValueWeightsResp/ScoreWeightsResp
声明了必填元字段（range/maxes/base_total/source），而 POST 保存与 reset 的
历史返回只有权重 dict → FastAPI ResponseValidationError → 前端「保存权重 报错」。
本测试直接调用路由函数并用 pydantic 校验返回，防止同类漏网复发。
"""
from __future__ import annotations

import asyncio

from backend import api
from backend.schemas import ScoreWeightsResp, ValueWeightsResp


def test_value_weights_endpoints_schema() -> None:
    get = asyncio.run(api.value_weights_get())
    ValueWeightsResp(**get)  # GET：全元信息

    saved = asyncio.run(api.value_weights_save({"finance": 1.2, "board": 0.8}))
    assert saved["ok"] is True and saved["finance"] == 1.2 and saved["board"] == 0.8
    ValueWeightsResp(**saved)  # 不抛即通过（曾因缺 range/maxes/base_total/source 500）

    reset = asyncio.run(api.value_weights_reset())
    ValueWeightsResp(**reset)
    assert reset["finance"] == 1.0  # reset 后回默认

    # 非法输入 clamp 兜底
    clamped = asyncio.run(api.value_weights_save({"finance": 999, "board": "abc"}))
    ValueWeightsResp(**clamped)
    assert clamped["finance"] == 3.0  # clamp 到 _MAX
    assert clamped["board"] == 1.0    # 非法值回退当前值


def test_score_weights_endpoints_schema() -> None:
    get = asyncio.run(api.score_weights_get())
    ScoreWeightsResp(**get)

    saved = asyncio.run(api.score_weights_save({"tech": 1.5}))
    ScoreWeightsResp(**saved)
    assert saved["ok"] is True

    reset = asyncio.run(api.score_weights_reset())
    ScoreWeightsResp(**reset)
