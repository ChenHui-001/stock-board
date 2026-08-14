"""全局配置：全部通过环境变量注入，便于 Docker 部署。"""
from __future__ import annotations

import os
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


class Settings:
    # ---------- 基础 ----------
    APP_NAME = "股票看板"
    TZ = os.getenv("TZ", "Asia/Shanghai")
    # Docker 内由 Dockerfile 设为 /app/data；本地开发落到项目下的 ./data
    DATA_DIR = Path(os.getenv("DATA_DIR") or (Path(__file__).resolve().parent.parent / "data"))
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = _int("PORT", 8000)

    # ---------- 数据源 ----------
    # 顺序即故障转移优先级，东方财富为主，其余为辅
    PROVIDER_ORDER = _list(
        "PROVIDER_ORDER", ["eastmoney", "tencent", "sina", "ths", "akshare"]
    )
    ENABLE_AKSHARE = _bool("ENABLE_AKSHARE", False)
    HTTP_TIMEOUT = _float("HTTP_TIMEOUT", 6.0)
    HTTP_RETRY = _int("HTTP_RETRY", 2)

    # ---------- 缓存 TTL（秒）----------
    QUOTE_TTL_OPEN = _float("QUOTE_TTL_OPEN", 2.5)      # 盘中实时行情
    QUOTE_TTL_CLOSED = _float("QUOTE_TTL_CLOSED", 60.0)  # 盘后
    HISTORY_TTL_OPEN = _float("HISTORY_TTL_OPEN", 120.0)  # K线/资金/两融 盘中
    HISTORY_TTL_CLOSED = _float("HISTORY_TTL_CLOSED", 900.0)
    SEARCH_TTL = _float("SEARCH_TTL", 300.0)
    HOT_TTL = _float("HOT_TTL", 60.0)

    # ---------- LLM（OpenAI 兼容协议）----------
    LLM_ENABLED = _bool("LLM_ENABLED", True)
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
    LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
    LLM_TIMEOUT = _float("LLM_TIMEOUT", 45.0)
    LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 4000)
    LLM_TEMPERATURE = _float("LLM_TEMPERATURE", 0.25)
    # 部分兼容端点不支持 response_format=json_object，可关闭
    LLM_JSON_MODE = _bool("LLM_JSON_MODE", True)

    # ---------- 业务参数 ----------
    KLINE_LIMIT = _int("KLINE_LIMIT", 260)   # 需覆盖 MA60 与 30 日趋势
    FLOW_DAYS = _int("FLOW_DAYS", 30)
    MARGIN_DAYS = _int("MARGIN_DAYS", 30)

    @property
    def db_path(self) -> Path:
        return self.DATA_DIR / "board.db"


settings = Settings()
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
