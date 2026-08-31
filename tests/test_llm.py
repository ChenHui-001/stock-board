"""Llm。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from backend import (
    analysis, api, cache, check_sources, hotspot, hotspot_ai, hotspot_search,
    indicators, llm, llmcfg, metrics, news, providers, reports, scorecfg,
    service, storage, value_screener, valuecfg,
)
from backend.config import settings
from backend.indicators import build_ma, summarize_flow, support_resistance
from backend.providers import registry
from backend.providers.base import Bar, FlowDay

def test_json_repair() -> None:
    cases: dict[str, str] = {
        '{"action": "持有观望", "confidence": 80}': "持有观望",
        '```json\n{"action": "积极持仓/加仓"}\n```': "积极持仓/加仓",
        '```JSON\n{"action": "清仓离场"}\n```': "清仓离场",
        "前文寒暄 {\"action\": \"减仓规避\"} 结尾废话": "减仓规避",
        '{"action": "清仓离场", "list": [1, 2,]}': "清仓离场",
        '{"action": "持有观望", "trend": {"summary": "多头", "short": "截断': "持有观望",
        '{"advice": {"action": "持有观望", "reason": "依据", "support": 12.34': "持有观望",
        '{"action": "减仓规避", "a": {"b": [1,2,3], "c": {"d": 1}': "减仓规避",
        '{"action": "持有观望", "x": 1}{"action": "减仓规避"}': "持有观望",
    }
    for raw, want in cases.items():
        try:
            obj = llm._extract_json(raw)
            got = obj.get("action") or (obj.get("advice") or {}).get("action")
            assert (got == want), f"got={got!r}"
        except llm.LLMError as exc:
            assert (False), str(exc)
    try:
        llm._extract_json("这不是 JSON 内容")
        assert (False)
    except llm.LLMError:
        assert (True)





def test_llm_timeout_floor() -> None:
    """LLM 超时下限：旧 .env/compose 残留的 45s 等过小配置不得掐断正常生成。"""
    from backend.config import _clamp_llm_timeout

    assert (_clamp_llm_timeout(45.0) == 90.0)
    assert (_clamp_llm_timeout(0.0) == 90.0)
    assert (_clamp_llm_timeout(90.0) == 90.0)
    assert (_clamp_llm_timeout(120.0) == 120.0)
    assert (settings.LLM_TIMEOUT >= 90.0), f"got={settings.LLM_TIMEOUT}"





def test_fingerprint() -> None:
    """指纹随配置变化。

    本测试会写入 llm_config，因此必须先确认落在临时库上（见文件头的 DATA_DIR 断言），
    并在结束时还原原值——直接 clear 会抹掉使用者保存的真实 API Key。
    """
    storage.init_db()
    saved = storage.get_kv("llm_config")
    try:
        f1 = llmcfg.fingerprint()
        storage.set_llm_config({"enabled": True, "base_url": "https://x/v1", "model": "m1", "api_key": "k", "json_mode": True})
        f2 = llmcfg.fingerprint()
        storage.set_llm_config({"enabled": True, "base_url": "https://x/v1", "model": "m2", "api_key": "k", "json_mode": True})
        f3 = llmcfg.fingerprint()
        assert (f1 != f2 and f2 != f3)
    finally:
        if saved:
            storage.set_kv("llm_config", saved)
        else:
            storage.clear_llm_config()





def test_llm_profiles() -> None:
    """多档案：保存/主模型唯一/密钥保留/迁移/指纹（落在临时库上，结束还原）。"""
    storage.init_db()
    saved_profiles = storage.get_kv("llm_profiles")
    saved_legacy = storage.get_kv("llm_config")
    try:
        # 1) 保存两份档案，未标记主模型时第一个启用的自动设为主
        clean = llmcfg.save_profiles([
            {"id": "a", "name": "A", "enabled": True, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a", "api_key": "ka", "json_mode": True},
            {"id": "b", "name": "B", "enabled": True, "vendor": "custom",
             "base_url": "https://b/v1", "model": "m-b", "api_key": "kb", "json_mode": False},
        ])
        assert (len(clean) == 2), str(clean)
        assert (clean[0]["primary"] is True and clean[1]["primary"] is False), str([(p["name"], p["primary"]) for p in clean])
        assert (llmcfg.get_config()["id"] == "a")
        assert (llm.available() is True)

        # 2) 主模型唯一：再存时显式标记 b 为主，a 取消
        clean = llmcfg.save_profiles([
            {"id": "a", "name": "A", "enabled": True, "primary": False, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a", "api_key": "", "json_mode": True},
            {"id": "b", "name": "B", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://b/v1", "model": "m-b", "api_key": "", "json_mode": False},
        ])
        assert (clean[1]["primary"] is True and clean[0]["primary"] is False), str([(p["name"], p["primary"]) for p in clean])
        # api_key 传空且档案已存在 → 保留原密钥
        assert (clean[0]["api_key"] == "ka" and clean[1]["api_key"] == "kb"), str([p["api_key"] for p in clean])
        # clear_key=True 清空
        clean = llmcfg.save_profiles([
            {"id": "a", "name": "A", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a", "api_key": "x", "clear_key": True,
             "json_mode": True},
        ])
        assert (clean[0]["api_key"] == ""), str(clean)

        # 3) 旧单配置迁移：清空多档案，写旧 llm_config，get_profiles 应得到一份主档案
        storage.delete_kv("llm_profiles")
        storage.set_llm_config({"enabled": True, "base_url": "https://old/v1", "model": "m-old",
                                "api_key": "k-old", "json_mode": True})
        migrated = llmcfg.get_profiles()
        assert (len(migrated) == 1 and migrated[0]["primary"] is True
              and migrated[0]["base_url"] == "https://old/v1" and migrated[0]["model"] == "m-old"), str(migrated)

        # 4) 指纹联动：多档案任一变化指纹即变
        llmcfg.save_profiles([
            {"id": "a", "name": "A", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a", "api_key": "ka", "json_mode": True},
        ])
        fp1 = llmcfg.fingerprint()
        llmcfg.save_profiles([
            {"id": "a", "name": "A", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a2", "api_key": "ka", "json_mode": True},
        ])
        fp2 = llmcfg.fingerprint()
        assert (fp1 != fp2), f"{fp1} vs {fp2}"

        # 5) 故障转移顺序：主模型在前
        llmcfg.save_profiles([
            {"id": "b", "name": "B", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://b/v1", "model": "m-b", "api_key": "kb", "json_mode": True},
            {"id": "a", "name": "A", "enabled": True, "primary": False, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a", "api_key": "ka", "json_mode": True},
        ])
        order = llm.ordered_profiles()
        assert ([p["id"] for p in order] == ["b", "a"]), str([p["id"] for p in order])

        # 6) merge_pending（测试连接）：按 id 匹配档案为基底，api_key 留空保留已存密钥
        #    主模型是 b，测试备选 a 时不能拿 b 的字段、也不能把 a 的密钥覆盖成空
        merged = llmcfg.merge_pending({"id": "a", "name": "A", "enabled": True,
                                       "primary": False, "vendor": "custom",
                                       "base_url": "https://a/v1", "model": "m-a",
                                       "api_key": "", "json_mode": True})
        assert (merged["base_url"] == "https://a/v1"
              and merged["model"] == "m-a"), str(merged)
        assert (merged["api_key"] == "ka"), str(merged)
        merged2 = llmcfg.merge_pending({"id": "a", "vendor": "custom",
                                        "base_url": "https://a/v1", "model": "m-a",
                                        "api_key": "new-key", "json_mode": True})
        assert (merged2["api_key"] == "new-key"), str(merged2)
        merged3 = llmcfg.merge_pending({"id": "no-such", "vendor": "custom",
                                        "base_url": "https://x/v1", "model": "m-x",
                                        "api_key": "", "json_mode": True})
        assert (merged3["id"] == "b"), str(merged3)
    finally:
        if saved_profiles is not None:
            storage.set_kv("llm_profiles", saved_profiles)
        else:
            storage.delete_kv("llm_profiles")
        if saved_legacy is not None:
            storage.set_kv("llm_config", saved_legacy)
        else:
            storage.clear_llm_config()





def test_llm_failover() -> None:
    """chat_json 故障转移：主模型失败自动切换下一个，全部失败汇总原因。"""
    storage.init_db()
    saved_profiles = storage.get_kv("llm_profiles")
    saved_legacy = storage.get_kv("llm_config")
    orig_chat_one = llm._chat_one
    try:
        llmcfg.save_profiles([
            {"id": "bad", "name": "坏模型", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://bad/v1", "model": "m-bad", "api_key": "kb", "json_mode": True},
            {"id": "good", "name": "好模型", "enabled": True, "primary": False, "vendor": "custom",
             "base_url": "https://good/v1", "model": "m-good", "api_key": "kg", "json_mode": True},
        ])
        calls: list[str] = []

        async def fake_chat_one(cfg, system, user):
            calls.append(cfg["id"])
            if cfg["id"] == "bad":
                raise llm.LLMError("超时")
            return {"ok": 1}, {"model": cfg["model"]}

        llm._chat_one = fake_chat_one
        data, meta = asyncio.run(llm.chat_json("s", "u"))
        assert (calls == ["bad", "good"] and data == {"ok": 1}), f"calls={calls}"
        assert (meta.get("model") == "m-good"), str(meta)

        # 全部失败 → 汇总各档案原因
        async def fake_all_fail(cfg, system, user):
            raise llm.LLMError("挂了")

        llm._chat_one = fake_all_fail
        try:
            asyncio.run(llm.chat_json("s", "u"))
            assert (False), "未抛错"
        except llm.LLMError as exc:
            msg = str(exc)
            assert ("坏模型" in msg and "好模型" in msg), msg
    finally:
        llm._chat_one = orig_chat_one
        if saved_profiles is not None:
            storage.set_kv("llm_profiles", saved_profiles)
        else:
            storage.delete_kv("llm_profiles")
        if saved_legacy is not None:
            storage.set_kv("llm_config", saved_legacy)
        else:
            storage.clear_llm_config()


