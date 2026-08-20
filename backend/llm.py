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
from .utils import describe_exc

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


def _request_error(exc: httpx.HTTPError, url: str) -> str:
    """把 httpx 的网络异常翻译成可执行提示。

    httpx 的超时类异常 str() 通常为空（由 anyio 的无参 TimeoutError 转换而来），
    直接插值会得到「LLM 请求失败: 」这种没有任何信息的报错，因此这里统一
    补上异常类型与目标主机。
    """
    host = httpx.URL(url).host or url
    timeout = settings.LLM_TIMEOUT
    if isinstance(exc, httpx.ConnectTimeout):
        return (
            f"连接 {host} 超时（{timeout:.0f}s）。请检查 Base URL 是否填对、"
            "本机能否访问该地址（代理/防火墙）"
        )
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"等待 {host} 响应超时（{timeout:.0f}s）。请调大 LLM_TIMEOUT 环境变量，"
            "或换用更快的模型"
        )
    detail = str(exc).strip()
    if isinstance(exc, httpx.ConnectError):
        return f"无法连接 {host}（{describe_exc(exc)}）。请检查 Base URL 与网络"
    return f"{type(exc).__name__}{'：' + detail if detail else ''}（目标 {host}）"


# 空 content / JSON 解析失败时的总尝试次数
MAX_ATTEMPTS = 3

# 云端 /models 返回的模型 ID 中，明确非「对话补全」类别的关键字（embedding/图片/语音/重排等），
# 下拉选择时直接过滤，避免选中后 /chat/completions 必失败
_NON_CHAT_RE = re.compile(
    r"embed|bge|m3e|jina|e5-|gte|text-embedding|dall|image|sdxl|stable-diffusion|flux|"
    r"tts|speech|audio|whisper|rerank|re-rank|moderat|vector|video",
    re.I,
)


def _extract_json(text: str) -> dict[str, Any]:
    """解析模型返回的 JSON，容忍代码块围栏、前后寒暄、尾随逗号与截断。

    修复阶梯：原文 -> 平衡括号提取首个完整对象 -> 去尾随逗号 -> 截断前缀。
    """
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回为空")
    fence = re.search(r"```(?:json|JSON)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()

    def _loads(s: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    # 1) 原文直接解析
    obj = _loads(text)
    if obj is not None:
        return obj

    # 2) 平衡括号提取首个完整对象（容忍前后寒暄、多个对象拼接）
    depth = 0
    start = -1
    first_start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
                if first_start < 0:
                    first_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                obj = _loads(text[start : i + 1])
                if obj is not None:
                    return obj
    # 首个对象片段解析失败（或对象未闭合）：对该片段做修复
    if first_start >= 0:
        tail = text[first_start:]
        # 3) 常见小毛病：尾随逗号（仅当修复后能通过 json.loads 校验才采用）
        repaired = _loads(_strip_trailing_commas(tail))
        if repaired is not None:
            return repaired
        # 4) 截断修复：取最长可解析前缀，必要时补齐闭合括号
        repaired = _repair_truncated(tail)
        if repaired is not None:
            return repaired
    raise LLMError(f"模型返回不是合法 JSON（内容开头：{text[:100]!r}）")


def _strip_trailing_commas(s: str) -> str:
    """移除所有尾随逗号（如 [1, 2,] -> [1, 2]，{"a":1,} -> {"a":1}）。

    结果必须通过 json.loads 校验才被采用，因此误伤字符串内容时不会产生错误结果。
    """
    return re.sub(r",\s*([}\]])", r"\1", s)


def _repair_truncated(s: str) -> dict[str, Any] | None:
    """截断修复：取最长可解析前缀；必要时收紧/补齐闭合括号与尾随逗号。"""
    closes = [i for i, ch in enumerate(s) if ch in "}]"]
    suffix_pool = ("", "}", "]}", "}}", "])", "]}}", "}}}]")
    candidates: list[str] = []
    # 从右往左：先试最长的前缀（最近的闭合符），命中即最长完整结构
    for i in closes[-16:][::-1]:
        for suf in suffix_pool:
            candidates.append(s[: i + 1] + suf)
    # 截断发生在未闭合字符串内部（如 "short": "截断）：先补闭合引号再补括号；
    # 所有候选都经 json.loads 校验，误补不会产生错误结果
    for suf in suffix_pool[1:]:
        candidates.append(s + '"' + suf)
    for suf in suffix_pool[1:]:
        candidates.append(s.rstrip() + suf)
    for cand in candidates:
        try:
            obj = json.loads(_strip_trailing_commas(cand))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


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

    # 思考类模型（deepseek-reasoner / *-thinking / r1 等）的思考过程也占用输出配额，
    # 按模型名预先放大 max_tokens，避免思考挤占正文配额导致 content 为空
    thinking_bumped = False
    if re.search(r"reason|r1(?:\b|-)|think", c["model"], re.I):
        if settings.LLM_THINKING_MAX_TOKENS > payload["max_tokens"]:
            payload["max_tokens"] = settings.LLM_THINKING_MAX_TOKENS
            thinking_bumped = True

    url = f"{c['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {c['api_key']}",
        "Content-Type": "application/json",
    }

    async def post() -> httpx.Response:
        try:
            return await _client_of().post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败: {_request_error(exc, url)}") from exc

    async def post_with_400_repair() -> httpx.Response:
        """发请求；遇 400 按报错内容逐项自愈。

        每轮都重新读取本轮响应体再决定下一步，否则会拿上一轮的报错内容
        去判断这一轮的结果，导致该触发的修复被跳过。
        """
        nonlocal thinking_bumped
        resp = await post()
        for _ in range(2):
            if resp.status_code != 400:
                break
            body = resp.text[:300]
            if "response_format" in body and "response_format" in payload:
                # 端点不支持 json_object：去掉该字段重试
                payload.pop("response_format", None)
            elif "max_tokens" in body and thinking_bumped:
                # 放大的 max_tokens 超出端点上限：退回原值重试
                payload["max_tokens"] = settings.LLM_MAX_TOKENS
                thinking_bumped = False
            else:
                break
            resp = await post()
        return resp

    # 空 content 或 JSON 解析失败自动重试；第二次视失败原因追加针对性提示
    nudge = (
        "\n\n（注意：上一次返回的内容不是合法 JSON。请务必只输出一个完整的 JSON 对象："
        "不要包含 Markdown 代码块标记、注释或任何额外文字，所有字符串用双引号，"
        "字段之间用逗号分隔，不要截断。）"
    )
    thinking_nudge = (
        "\n\n（注意：请先完成思考过程，然后把最终的 JSON 结果完整输出在 content 正文中，"
        "不要只输出思考、不要截断。）"
    )
    last_finish: str | None = None
    last_msg: dict[str, Any] | None = None
    last_content: str = ""
    nudged_json = False
    for _attempt in range(1, MAX_ATTEMPTS + 1):
        if _attempt == 2 and (last_msg or {}).get("reasoning_content"):
            # 思考模型正文为空：放大配额并提示把结果输出到 content
            if settings.LLM_THINKING_MAX_TOKENS > payload["max_tokens"]:
                payload["max_tokens"] = settings.LLM_THINKING_MAX_TOKENS
                thinking_bumped = True
            payload["messages"][1]["content"] = (
                str(payload["messages"][1]["content"]) + thinking_nudge
            )
        elif _attempt >= 2 and not nudged_json:
            payload["messages"][1]["content"] = str(payload["messages"][1]["content"]) + nudge
            nudged_json = True

        resp = await post_with_400_repair()
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
            last_content = content
            try:
                parsed = _extract_json(content)
            except LLMError as exc:
                log.warning(
                    "LLM JSON 解析失败（attempt %s/%s）：%s，内容开头：%s",
                    _attempt, MAX_ATTEMPTS, exc, content[:120].replace("\n", " "),
                )
                continue
            meta = {
                "model": data.get("model") or c["model"],
                "usage": data.get("usage") or {},
            }
            return parsed, meta
        last_finish = choices[0].get("finish_reason")
        last_msg = msg
        log.warning(
            "LLM 返回空 content（attempt %s/%s），finish_reason=%s，reasoning_len=%s，message=%s",
            _attempt, MAX_ATTEMPTS, last_finish,
            len(str(msg.get("reasoning_content") or "")), str(last_msg)[:200],
        )

    if last_content:
        raise LLMError(f"模型返回不是合法 JSON（内容开头：{last_content[:100]!r}）")
    raise LLMError(_empty_content_error(last_finish, last_msg, thinking_bumped))


def _empty_content_error(
    finish: str | None, msg: dict[str, Any] | None, thinking_bumped: bool = False
) -> str:
    """把空响应的常见原因翻译成可执行的提示。"""
    if (msg or {}).get("reasoning_content"):
        if thinking_bumped:
            return (
                "模型只输出了思考过程、未输出正文（content 为空），自动调大输出配额"
                "（LLM_THINKING_MAX_TOKENS）后仍失败。请在 ⚙ 设置里调大"
                "LLM_THINKING_MAX_TOKENS，或换用非思考类模型（如 deepseek-chat）后重试"
            )
        return (
            "模型只输出了思考过程、未输出正文（content 为空），已自动调大输出配额重试。"
            "请换用非思考类模型（如 deepseek-chat 而非 deepseek-reasoner），"
            "或在环境变量里调大 LLM_THINKING_MAX_TOKENS 后重试"
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
        return False, f"连接失败：{_request_error(exc, url)}"
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
        return False, [], f"获取失败：{_request_error(exc, url)}"
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
    filtered = 0
    for row in data.get("data") or []:
        mid = (row or {}).get("id")
        if not mid:
            continue
        mid = str(mid)
        if _NON_CHAT_RE.search(mid):
            filtered += 1
            continue
        models.append(mid)
    if not models:
        return False, [], (
            f"接口共返回 {len(data.get('data') or [])} 个模型，但都不是对话补全类"
            "（已过滤 embedding/图片/语音等），无法用于 AI 分析"
        )
    note = f"获取到 {len(models)} 个模型"
    if filtered:
        note += f"（已过滤 {filtered} 个非对话模型）"
    return True, sorted(set(models)), note
