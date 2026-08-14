"""运行时 LLM 配置：数据库覆盖 + 环境变量兜底，支持界面自定义厂商/模型/密钥。

优先级：界面保存的配置（kv 表） > 环境变量（.env / settings）。
"""
from __future__ import annotations

from typing import Any

from . import storage
from .config import settings

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

_FIELDS = ("enabled", "vendor", "base_url", "model", "api_key", "json_mode")


def get_config() -> dict[str, Any]:
    """当前生效配置：DB 覆盖优先，未存过的字段用环境变量兜底。"""
    cfg: dict[str, Any] = {
        "enabled": settings.LLM_ENABLED,
        "vendor": "custom",
        "base_url": settings.LLM_BASE_URL,
        "model": settings.LLM_MODEL,
        "api_key": settings.LLM_API_KEY,
        "json_mode": settings.LLM_JSON_MODE,
    }
    stored = storage.get_llm_config()
    for key in _FIELDS:
        if key in stored and stored[key] not in (None, ""):
            cfg[key] = stored[key]
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """保存配置。api_key 传空保留原值；clear_key=True 时清空密钥。"""
    current = get_config()
    clean: dict[str, Any] = {}
    for key in _FIELDS:
        if key in cfg and cfg[key] is not None:
            clean[key] = cfg[key]

    preset = VENDORS.get(str(clean.get("vendor") or "custom"))
    if preset:
        for key in ("base_url", "model"):
            if not str(clean.get(key) or "").strip():
                clean[key] = preset[key]

    if clean.get("base_url"):
        clean["base_url"] = str(clean["base_url"]).strip().rstrip("/")

    if cfg.get("clear_key"):
        clean["api_key"] = ""
    elif not str(clean.get("api_key") or "").strip():
        clean["api_key"] = current.get("api_key") or ""

    storage.set_llm_config(clean)
    return clean


def merge_pending(cfg: dict[str, Any]) -> dict[str, Any]:
    """把界面表单（尚未保存）的字段合并到当前配置，用于「测试连接」。"""
    merged = dict(get_config())
    for key in _FIELDS:
        if key in cfg and cfg[key] not in (None, ""):
            merged[key] = cfg[key]
    if cfg.get("clear_key"):
        merged["api_key"] = ""
    return merged


def reset_config() -> dict[str, Any]:
    """清除界面配置，回退到环境变量。"""
    storage.clear_llm_config()
    return get_config()
