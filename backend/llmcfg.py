"""运行时 LLM 配置：多模型档案（profiles）+ 环境变量兜底，支持自动故障转移。

优先级：界面保存的多档案（kv 表 llm_profiles） > 环境变量（.env / settings）。

多档案语义：
- 每份档案 = 一套厂商/Base URL/模型/密钥/JSON 模式/启用开关；
- 其中一份标记为「主模型」（primary），调用时主模型优先，其余按保存顺序
  依次作为故障转移备选：主模型超时/报错自动切换下一个，全部失败才降级
  内置规则引擎；
- 旧版单一配置（kv 表 llm_config）首次读取时自动迁移为一份主档案。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from . import storage
from .config import settings

log = logging.getLogger("llmcfg")

# 厂商预设：界面下拉「厂商预设」选择后自动带出 Base URL / 模型 / JSON 模式
VENDORS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "json_mode": True,
    },
    "qwen": {
        "name": "阿里通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "json_mode": True,
    },
    "kimi": {
        "name": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "json_mode": True,
    },
    "glm": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-plus",
        "json_mode": True,
    },
    "ollama": {
        "name": "本地 Ollama",
        "base_url": "http://host.docker.internal:11434/v1",
        "model": "qwen2.5:14b",
        "json_mode": False,
    },
    "custom": {
        "name": "自定义",
        "base_url": "",
        "model": "",
        "json_mode": True,
    },
}

# 档案字段（不含由后端维护的 id / api_key_set 派生标记）
_FIELDS = ("enabled", "primary", "vendor", "base_url", "model", "api_key", "json_mode", "name")
_KEY = "llm_profiles"
_LEGACY_KEY = "llm_config"


def _env_profile() -> dict[str, Any]:
    """环境变量合成一份主档案（未配置界面档案时的兜底）。"""
    return {
        "id": "env",
        "name": "环境变量",
        "enabled": settings.LLM_ENABLED,
        "primary": True,
        "vendor": "custom",
        "base_url": settings.LLM_BASE_URL,
        "model": settings.LLM_MODEL,
        "api_key": settings.LLM_API_KEY,
        "json_mode": settings.LLM_JSON_MODE,
    }


def _new_id(profiles: list[dict[str, Any]]) -> str:
    used = {str(p.get("id") or "") for p in profiles}
    for _ in range(64):
        nid = "p" + uuid.uuid4().hex[:8]
        if nid not in used:
            return nid
    return "p" + str(int(time.time() * 1000))


def _norm(p: dict[str, Any], idx: int = 0) -> dict[str, Any]:
    """规范化一份档案：默认值 + 类型强制 + 厂商预设补齐。"""
    preset = VENDORS.get(str(p.get("vendor") or "custom")) or VENDORS["custom"]
    out: dict[str, Any] = {
        "id": str(p.get("id") or ""),
        "name": str(p.get("name") or "").strip() or f"模型 {idx + 1}",
        "enabled": bool(p.get("enabled", True)),
        "primary": bool(p.get("primary", False)),
        "vendor": str(p.get("vendor") or "custom"),
        "base_url": str(p.get("base_url") or "").strip().rstrip("/"),
        "model": str(p.get("model") or "").strip(),
        "api_key": str(p.get("api_key") or ""),
        "json_mode": bool(p.get("json_mode", preset["json_mode"])),
    }
    # 厂商预设：Base URL / 模型留空时自动带出
    if not out["base_url"] and preset["base_url"]:
        out["base_url"] = preset["base_url"]
    if not out["model"] and preset["model"]:
        out["model"] = preset["model"]
    return out


def get_profiles() -> list[dict[str, Any]]:
    """当前生效的多档案：界面保存的优先，未配置时环境变量合成一份。

    旧版单一配置（llm_config）首次读取时迁移为一份主档案，保证升级无缝。
    """
    raw = storage.get_kv(_KEY)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [_norm(p, i) for i, p in enumerate(data)]
        except json.JSONDecodeError:
            log.warning("llm_profiles 解析失败，回退环境变量：%s", raw[:80])

    legacy = storage.get_llm_config()
    if legacy:
        legacy = {k: v for k, v in legacy.items() if k in _FIELDS}
        legacy.setdefault("enabled", settings.LLM_ENABLED)
        legacy.setdefault("vendor", "custom")
        legacy.setdefault("name", "模型 1")
        legacy["primary"] = True
        return [_norm(legacy, 0)]

    return [_env_profile()]


def get_config() -> dict[str, Any]:
    """当前生效配置：主模型档案（用于 meta/测试连接等单档案场景）。"""
    profiles = get_profiles()
    for p in profiles:
        if p["primary"] and p["enabled"]:
            return p
    for p in profiles:
        if p["enabled"]:
            return p
    return profiles[0] if profiles else _env_profile()


def save_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保存多档案。返回规范化后的列表。

    - 前端不回显 api_key：传空且档案已存在时保留原密钥（按 id 匹配）；
    - clear_key=True 时清空该档案密钥；
    - 未标记主模型时，第一个启用的档案自动设为主模型。
    """
    current = get_profiles()
    cur_by_id = {str(p.get("id") or ""): p for p in current}

    clean: list[dict[str, Any]] = []
    for i, p in enumerate(profiles):
        if not isinstance(p, dict):
            continue
        nid = str(p.get("id") or "").strip()
        is_new = not nid or nid not in cur_by_id
        norm = _norm(p, i)
        if is_new:
            norm["id"] = nid or _new_id(clean)
        else:
            norm["id"] = nid
            if p.get("clear_key"):
                norm["api_key"] = ""
            elif not str(p.get("api_key") or "").strip():
                norm["api_key"] = cur_by_id[nid].get("api_key") or ""
        clean.append(norm)

    # 未标记主模型：第一个启用的档案自动设为主模型
    if not any(p["primary"] for p in clean):
        for p in clean:
            if p["enabled"]:
                p["primary"] = True
                break
    # 有标记主模型：保证唯一
    else:
        seen_primary = False
        for p in clean:
            if p["primary"]:
                if seen_primary:
                    p["primary"] = False
                else:
                    seen_primary = True

    storage.set_kv(_KEY, json.dumps(clean, ensure_ascii=False))
    return clean


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """兼容旧接口：保存单一配置（作为一份主档案）。"""
    clean = save_profiles([cfg])
    return clean[0] if clean else _env_profile()


def merge_pending(cfg: dict[str, Any]) -> dict[str, Any]:
    """把界面表单（尚未保存）的字段合并到当前配置，用于「测试连接」。

    cfg 携带 id 且匹配已存档案时以该档案为基底（否则用主模型档案）；
    界面不回显密钥，api_key 留空时保留基底档案已存密钥（与 save_profiles 同规则），
    避免测试已保存的备选档案时把密钥覆盖成空导致「请先填写 Base URL、模型与 API Key」。
    """
    profiles = get_profiles()
    nid = str(cfg.get("id") or "").strip()
    base: dict[str, Any] = get_config()
    if nid:
        for p in profiles:
            if str(p.get("id") or "") == nid:
                base = p
                break
    merged = dict(base)
    for key in _FIELDS:
        if key in cfg and cfg[key] is not None:
            merged[key] = cfg[key]
    if not str(merged.get("api_key") or "").strip() and base.get("api_key"):
        merged["api_key"] = base["api_key"]
    if cfg.get("clear_key"):
        merged["api_key"] = ""
    return merged


def fingerprint() -> str:
    """当前生效 LLM 配置的指纹，用于判断 AI 当日缓存是否仍适用。

    多档案下，任一启用档案的 Base URL / 模型 / 是否配置 Key 变化，指纹即不同：
    旧缓存应作废重新分析，避免用户配好大模型后仍看到规则引擎的降级结果。
    """
    parts: list[str] = []
    for p in get_profiles():
        if not p["enabled"]:
            continue
        parts.append("|".join([
            "1" if p["primary"] else "0",
            str(p.get("base_url") or ""),
            str(p.get("model") or ""),
            "1" if p.get("api_key") else "0",
        ]))
    return ";".join(parts) or "none"


def reset_config() -> dict[str, Any]:
    """清除界面配置（多档案 + 旧单配置），回退到环境变量。"""
    storage.delete_kv(_KEY)
    storage.clear_llm_config()
    return get_config()
