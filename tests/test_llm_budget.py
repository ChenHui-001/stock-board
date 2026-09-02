"""P0-6 回归测试：LLM 多档案串行故障转移必须受总预算约束。

守护两处代码：
  1. backend/llm.py `chat_json()` 里 asyncio.wait_for 的总预算包裹
  2. backend/config.py 的 LLM_TOTAL_TIMEOUT 与 _clamp_llm_total_timeout

为什么必须有这组测试：chat_json 按档案**串行**重试，单档案最坏跑满
LLM_TIMEOUT(120s)，4 个档案就是 480s（8 分钟），而调用方
backend/analysis/__init__.py 只 `except llm.LLMError`，没有外层超时。
所以总预算耗尽时抛出的类型必须是 LLMError——抛裸的 asyncio.TimeoutError
会绕过降级分支、直接把请求打挂（用户看到转圈而不是规则引擎结论）。

所有用例都把 LLM_TOTAL_TIMEOUT 压到 0.1~0.3s、_chat_one 换成 sleep，
不真等 180 秒。

注意：每个异步用例都显式带 @pytest.mark.asyncio。tests/conftest.py 的
自动打标钩子用 `co_flags & 0x100` 判断协程，而 CO_COROUTINE 实际是 0x80
（0x100 是 CO_ITERABLE_COROUTINE），导致 async def test_xxx 永远拿不到
asyncio 标记、被 pytest 静默 skip（不报错、不失败）。不要依赖那个钩子。
"""
from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any

import pytest

from backend import analysis, llm, llmcfg
from backend.config import settings


def _profile(name: str) -> dict[str, Any]:
    """满足 llm._usable 的最小档案配置（available() 会用它判定是否启用）。"""
    return {
        "name": name,
        "model": f"model-{name}",
        "api_key": "test-key",
        "base_url": "http://llm.test/v1",
        "enabled": True,
        "json_mode": True,
    }


async def _hang(profile: dict[str, Any], system: str, user: str) -> tuple[dict, dict]:
    """永不返回的档案：模拟 LLM 端点卡死（远超总预算）。"""
    await asyncio.sleep(30)
    return {"action": "观望"}, {}


# ---------------------------------------------------------------- 总预算类型与消息


@pytest.mark.asyncio
async def test_budget_exhausted_raises_llmerror_not_timeouterror(monkeypatch) -> None:
    """总预算耗尽必须抛 LLMError，不能是裸的 asyncio.TimeoutError。

    调用方 analysis 只 except llm.LLMError，抛别的类型会绕过降级分支。
    pytest.raises(llm.LLMError) 本身就守住了这一点：抛 TimeoutError 时
    该异常会直接冒泡、用例失败。
    """
    monkeypatch.setattr(settings, "LLM_TOTAL_TIMEOUT", 0.1)
    monkeypatch.setattr(llm, "ordered_profiles", lambda: [_profile("主源")])
    monkeypatch.setattr(llm, "_chat_one", _hang)

    t0 = time.monotonic()
    with pytest.raises(llm.LLMError) as ei:
        await llm.chat_json("sys", "user")
    elapsed = time.monotonic() - t0

    assert not isinstance(ei.value, asyncio.TimeoutError), type(ei.value)
    assert "总预算" in str(ei.value), str(ei.value)
    assert "耗尽" in str(ei.value), str(ei.value)
    # 单档案 sleep(30)，未加总预算时会挂满 30s
    assert elapsed < 5.0, f"耗时 {elapsed:.2f}s，总预算没生效"


@pytest.mark.asyncio
async def test_budget_timeout_keeps_failed_profiles_in_message(monkeypatch) -> None:
    """总预算耗尽时，已耗时档案的原因要留在异常消息里方便排障。

    场景：主源快速失败（进 errors），备源卡死被总预算掐断
    -> 两个档案的原因都应出现在消息中。
    """
    async def _flaky(profile: dict[str, Any], system: str, user: str) -> tuple[dict, dict]:
        if profile["name"] == "主源":
            raise llm.LLMError("等待 model-主源 响应超时（120s）")
        await asyncio.sleep(30)
        return {"action": "观望"}, {}

    monkeypatch.setattr(settings, "LLM_TOTAL_TIMEOUT", 0.3)
    monkeypatch.setattr(llm, "ordered_profiles",
                        lambda: [_profile("主源"), _profile("备源")])
    monkeypatch.setattr(llm, "_chat_one", _flaky)

    with pytest.raises(llm.LLMError) as ei:
        await llm.chat_json("sys", "user")

    msg = str(ei.value)
    assert "总预算" in msg and "耗尽" in msg, msg
    assert "主源" in msg, f"已失败的主源未出现在消息里：{msg}"
    assert "备源" in msg, f"被掐断的备源未出现在消息里：{msg}"


# ---------------------------------------------------------------- 不破坏既有行为


@pytest.mark.asyncio
async def test_failover_still_switches_profile_within_budget(monkeypatch) -> None:
    """总预算不能破坏正常的故障转移：主源失败后仍要切到备源并成功返回。"""
    calls: list[str] = []

    async def _flaky(profile: dict[str, Any], system: str, user: str) -> tuple[dict, dict]:
        calls.append(profile["name"])
        if profile["name"] == "主源":
            raise llm.LLMError("主源不可用")
        return {"action": "清仓离场"}, {"model": profile["model"]}

    monkeypatch.setattr(settings, "LLM_TOTAL_TIMEOUT", 5.0)
    monkeypatch.setattr(llm, "ordered_profiles",
                        lambda: [_profile("主源"), _profile("备源")])
    monkeypatch.setattr(llm, "_chat_one", _flaky)

    data, meta = await llm.chat_json("sys", "user")

    assert calls == ["主源", "备源"], calls
    assert data["action"] == "清仓离场", data
    assert meta["model"] == "model-备源", meta


@pytest.mark.asyncio
async def test_all_profiles_fail_fast_is_not_reported_as_budget(monkeypatch) -> None:
    """全档案快速失败走的是「全部失败」分支，不该被误报成总预算超时。"""
    async def _bad(profile: dict[str, Any], system: str, user: str) -> tuple[dict, dict]:
        raise llm.LLMError("boom")

    monkeypatch.setattr(settings, "LLM_TOTAL_TIMEOUT", 5.0)
    monkeypatch.setattr(llm, "ordered_profiles",
                        lambda: [_profile("源1"), _profile("源2")])
    monkeypatch.setattr(llm, "_chat_one", _bad)

    with pytest.raises(llm.LLMError) as ei:
        await llm.chat_json("sys", "user")

    msg = str(ei.value)
    assert "源1" in msg and "源2" in msg, msg
    assert "总预算" not in msg, f"快速失败被误报成超时：{msg}"


# ---------------------------------------------------------------- 端到端降级


def _detail() -> dict[str, Any]:
    """规则引擎所需的最小 detail 结构（字段口径对齐 test_analysis._mk_detail）。"""
    return {
        "quote": {"code": "600000", "name": "浦发银行", "price": 9.0,
                  "prev_close": 9.1, "change_pct": -1.1},
        "boards": [], "kline": [], "ma": [],
        "ma_summary": {"arrangement": "交织", "above_count": 0,
                       "above": [], "below": [], "series": {}},
        "support_resistance": {},
        "fund_flow": {"rows": [], "summary": {}},
        "margin": {"rows": [], "summary": {}},
        "financials": {"rows": []},
        "status": {"tags": [], "trend": {}},
    }


@pytest.mark.asyncio
async def test_analysis_degrades_to_rule_engine_when_budget_exhausted(monkeypatch) -> None:
    """端到端：LLM 卡死耗尽总预算后，analysis 仍返回结构完整的规则引擎结论。"""
    profiles = [_profile("主源")]
    monkeypatch.setattr(settings, "LLM_TOTAL_TIMEOUT", 0.1)
    monkeypatch.setattr(llm, "ordered_profiles", lambda: profiles)
    monkeypatch.setattr(llm, "_chat_one", _hang)
    # available() 走 llmcfg.get_profiles，不打桩会走「未配置 LLM_API_KEY」分支，
    # 那样测不到真正的超时降级
    monkeypatch.setattr(llmcfg, "get_profiles", lambda: profiles)
    assert llm.available(), "LLM 应判定为可用，否则测的是未配置分支"

    t0 = time.monotonic()
    result = await analysis.analyze(_detail())
    elapsed = time.monotonic() - t0

    meta = result["meta"]
    assert "总预算" in meta["degraded_reason"], meta
    assert "未配置" not in meta["degraded_reason"], meta
    assert meta["engine"] == "rule", meta
    assert meta["divergence"] == {"status": "degraded"}, meta
    assert elapsed < 5.0, f"耗时 {elapsed:.2f}s，总预算没生效"

    # 降级结果结构完整，可直出给前端
    out = result["analysis"]
    assert out["advice"]["action"], out["advice"]
    assert 0 <= out["advice"]["confidence"] <= 100, out["advice"]
    assert "trend" in out and "capital" in out and "risk" in out, list(out)


# ---------------------------------------------------------------- 配置项与下限


def test_clamp_llm_total_timeout_pure_function() -> None:
    """下限保护：总预算不得小于单档案超时，否则连第一个档案都会被掐断。"""
    from backend.config import _clamp_llm_total_timeout

    # 30 < 120 -> 抬到 120；90 < 120 -> 抬到 120
    assert _clamp_llm_total_timeout(30.0, 120.0) == 120.0
    assert _clamp_llm_total_timeout(90.0, 120.0) == 120.0
    # 等于 / 大于单档案超时则原样保留（用户可调大，但不能调小）
    assert _clamp_llm_total_timeout(120.0, 120.0) == 120.0
    assert _clamp_llm_total_timeout(600.0, 120.0) == 600.0
    # 单档案被压到 90 时，总预算下限同步下移
    assert _clamp_llm_total_timeout(10.0, 90.0) == 90.0


def test_llm_total_timeout_default_and_env_clamp(monkeypatch) -> None:
    """默认值 180 = 单档案 120 + 一次换源机会；环境变量配小了要被抬回。"""
    from backend import config

    assert settings.LLM_TOTAL_TIMEOUT == 180.0, settings.LLM_TOTAL_TIMEOUT
    assert settings.LLM_TOTAL_TIMEOUT >= settings.LLM_TIMEOUT, (
        f"总预算 {settings.LLM_TOTAL_TIMEOUT} 应能容下单档案 {settings.LLM_TIMEOUT}"
    )

    # 环境变量配成 30（远小于 LLM_TIMEOUT=120）-> reload 后应被抬到 120
    monkeypatch.setenv("LLM_TOTAL_TIMEOUT", "30")
    try:
        reloaded = importlib.reload(config)
        single = reloaded.settings.LLM_TIMEOUT
        assert single >= 90.0, single
        assert reloaded.settings.LLM_TOTAL_TIMEOUT == single, (
            f"配置 30 未被抬回单档案超时 {single}"
        )
    finally:
        # 还原：清掉环境变量再 reload，避免污染后续用例
        monkeypatch.delenv("LLM_TOTAL_TIMEOUT", raising=False)
        restored = importlib.reload(config)

    assert restored.settings.LLM_TOTAL_TIMEOUT == 180.0, (
        f"reload 还原失败，得到 {restored.settings.LLM_TOTAL_TIMEOUT}"
    )
