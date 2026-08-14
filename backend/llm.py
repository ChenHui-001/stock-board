"""OpenAI 兼容协议的 LLM 客户端。

可对接 DeepSeek / 通义 / Kimi / 智谱 / vLLM / Ollama 等任意兼容 `/chat/completions`
的服务，只需配置 LLM_BASE_URL + LLM_API_KEY + LLM_MODEL。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from . import llmcfg
from .config import settings

log = logging.getLogger("llm")

_client: httpx.AsyncClient | None = None


def _cfg() -> dict[str, Any]:
    return llmcfg.get_config()


def available() -> bool:
    c = _cfg()
    return bool(c["enabled"] and c["api_key"] and c["base_url"] and c["model"])


def _client_of() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(settings.LLM_TIMEOUT))
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class LLMError(Exception):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    """兼容模型在 JSON 外包了 ```json 代码块或前后寒暄的情况。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回为空")
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型返回不是合法 JSON: {exc}") from exc
    raise LLMError("模型返回中未找到 JSON 对象")


async def chat_json(system: str, user: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """调用 chat/completions 并解析出 JSON。返回 (数据, 元信息)。"""
    if not available():
        raise LLMError("未配置 LLM_API_KEY")

    c = _cfg()
    payload: dict[str, Any] = {
        "model": c["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": settings.LLM_TEMPERATURE,
        "max_tokens": settings.LLM_MAX_TOKENS,
        "stream": False,
    }
    if c["json_mode"]:
        payload["response_format"] = {"type": "json_object"}

    url = f"{c['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {c['api_key']}",
        "Content-Type": "application/json",
    }

    # 空 content 可能来自思考模型把配额耗在推理上、输出被截断或瞬时异常，重试一次
    last_finish: str | None = None
    last_msg: dict[str, Any] | None = None
    for _attempt in (1, 2):
        try:
            resp = await _client_of().post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from exc

        if resp.status_code >= 400:
            body = resp.text[:300]
            # 不支持 json_object 的端点会报 400，自动降级重试一次
            if c["json_mode"] and resp.status_code == 400 and "response_format" in body:
                payload.pop("response_format", None)
                try:
                    resp = await _client_of().post(url, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    raise LLMError(f"LLM 请求失败: {exc}") from exc
            if resp.status_code >= 400:
                raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:300]}")

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError("LLM 响应不是 JSON") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"LLM 未返回结果: {str(data)[:200]}")
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        # 少数端点把正文拆成 content 片段数组返回
        if isinstance(content, list):
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        if content.strip():
            meta = {
                "model": data.get("model") or c["model"],
                "usage": data.get("usage") or {},
            }
            return _extract_json(content), meta
        last_finish = choices[0].get("finish_reason")
        last_msg = msg
        log.warning(
            "LLM 返回空 content（attempt %s/2），finish_reason=%s，message=%s",
            _attempt, last_finish, str(last_msg)[:200],
        )

    raise LLMError(_empty_content_error(last_finish, last_msg))


def _empty_content_error(finish: str | None, msg: dict[str, Any] | None) -> str:
    """把空响应的常见原因翻译成可执行的提示。"""
    if (msg or {}).get("reasoning_content"):
        return (
            "模型只输出了思考过程、未输出正文（content 为空）。"
            "请换用非思考类模型（如 deepseek-chat 而非 deepseek-reasoner），"
            "或调大 LLM_MAX_TOKENS 后重试"
        )
    if finish == "length":
        return (
            "模型输出被截断（finish_reason=length）。请调大 LLM_MAX_TOKENS "
            "环境变量（如 4000）或换用更大上下文的模型后重试"
        )
    snippet = str(msg)[:160] if msg else ""
    return f"模型返回为空（finish_reason={finish}，响应片段：{snippet}）"


async def test_connection(cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """最小化调用 chat/completions，验证 Base URL / 模型 / API Key 可用性。

    cfg 传入界面未保存的表单配置（None 则用当前生效配置）。
    """
    c = cfg or _cfg()
    if not (c.get("base_url") and c.get("api_key") and c.get("model")):
        return False, "请先填写 Base URL、模型与 API Key"

    payload: dict[str, Any] = {
        "model": c["model"],
        "messages": [{"role": "user", "content": "请只回复：OK"}],
        "max_tokens": 8,
        "stream": False,
    }
    url = f"{str(c['base_url']).rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {c['api_key']}",
        "Content-Type": "application/json",
    }
    start = time.monotonic()
    try:
        resp = await _client_of().post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"连接失败：{exc}"
    cost_ms = (time.monotonic() - start) * 1000
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}：{resp.text[:200]}"
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return False, "响应不是合法 JSON"
    model = data.get("model") or c["model"]
    return True, f"连接成功，模型 {model}，耗时 {cost_ms:.0f}ms"


async def list_models(cfg: dict[str, Any] | None = None) -> tuple[bool, list[str], str]:
    """从云端拉取可用模型列表（OpenAI 兼容的 GET /models）。

    cfg 传入界面未保存的表单配置（None 则用当前生效配置）。
    返回 (是否成功, 模型名列表, 提示信息)。
    """
    c = cfg or _cfg()
    if not (c.get("base_url") and c.get("api_key")):
        return False, [], "请先填写 Base URL 与 API Key"

    url = f"{str(c['base_url']).rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"}
    try:
        resp = await _client_of().get(url, headers=headers)
    except httpx.HTTPError as exc:
        return False, [], f"获取失败：{exc}"
    if resp.status_code in (401, 403):
        return False, [], f"API Key 无效（HTTP {resp.status_code}）"
    if resp.status_code >= 400:
        return False, [], (
            f"该服务不支持模型列表接口（HTTP {resp.status_code}）：{resp.text[:150]}"
        )
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return False, [], "响应不是合法 JSON"

    models: list[str] = []
    for row in data.get("data") or []:
        mid = (row or {}).get("id")
        if mid:
            models.append(str(mid))
    if not models:
        return False, [], "接口未返回任何模型"
    return True, sorted(set(models)), f"获取到 {len(models)} 个模型"
