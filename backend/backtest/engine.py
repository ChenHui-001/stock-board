"""回测数据层：行情 / 财报 / 分钟线的取数与缓存。

两条取数通道：
  1. **westock-data CLI**（`engine` 默认）：结构化 Markdown 表格输出，适合需要
     长历史 + 财报披露日的策略（如评分阈值检验）。
  2. **进程内 providers**（`backend.providers.registry`）：复用生产数据源与熔断
     /限流逻辑，适合只需要日线或 5 分钟线的策略（盘口信号类）。

两通道都落盘缓存到 `data/backtest_cache/`，避免重复消耗数据源配额。
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
import logging
log = logging.getLogger("backtest.engine")

# backend/backtest/ → 仓库根
ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "backtest_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 预热：MA60 需要 60 根，额外留 1.5 倍余量
WARMUP_BARS = 90

# 前瞻窗口（交易日）：与生产策略「5-10 个交易日」持有期对齐
FORWARD_DAYS = (1, 3, 5, 10)
PRIMARY_DAYS = 5

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


# --------------------------------------------------------------------- westock CLI

def westock_cli() -> list[str]:
    """返回 westock-data 调用命令：优先 PATH / WESTOCK_BIN，否则回退到 skill 脚本 + node。"""
    if os.getenv("WESTOCK_BIN"):
        return [os.environ["WESTOCK_BIN"]]
    skill = Path(
        os.path.expanduser(
            "~/.workbuddy/plugins/marketplaces/experts/plugins/"
            "strategy-backtest-expert/skills/westock-data/scripts/index.js"
        )
    )
    node = os.getenv("NODE_BIN") or "node"
    if skill.exists():
        return [node, str(skill)]
    return ["westock-data"]


def run_cli(args: list[str], timeout: int = 90) -> str:
    """同步执行 CLI 并回显失败信息（失败返回空串，由调用方决定降级）。"""
    import subprocess

    cmd = westock_cli() + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"westock-data 调用失败: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"westock-data 返回 {proc.returncode}: {proc.stderr[:300]}")
    return proc.stdout


def parse_md_table(text: str) -> pd.DataFrame:
    """解析 westock-data 输出的 Markdown 表格。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return pd.DataFrame()
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for ln in lines[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) == len(header):
            rows.append(cells)
    return pd.DataFrame(rows, columns=header)


def to_num(v: Any) -> float | None:
    """把表格单元格转成 float，失败返回 None。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("%", "")
    if not _NUM_RE.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------------- 长历史日线

def load_kline(code: str, limit: int) -> pd.DataFrame:
    """取前复权日线（带本地缓存，避免重复打接口）。

    westock kline 输出为**倒序**（最新在前），这里统一翻转为升序；
    收盘列叫 `last`、换手列叫 `exchange`，不要按关键词猜。
    返回列：date / open / close / high / low / volume / turnover。
    """
    cache = CACHE_DIR / f"kline_{code}_{limit}.csv"
    if cache.exists():
        try:
            return pd.read_csv(cache)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 异常，按空数据继续: %s", "load_kline", exc)
            cache.unlink(missing_ok=True)
    out = run_cli([
        "kline", code, "--period", "day", "--limit", str(limit), "--fq", "qfq",
    ])
    df = parse_md_table(out)
    if df.empty:
        return df
    need = ["date", "open", "last", "high", "low"]
    if not all(c in df.columns for c in need):
        raise RuntimeError(f"westock kline 字段缺失，实际列: {list(df.columns)}")
    out_df = pd.DataFrame({
        "date": df["date"],
        "open": df["open"].map(to_num),
        "close": df["last"].map(to_num),
        "high": df["high"].map(to_num),
        "low": df["low"].map(to_num),
        "volume": df["volume"].map(to_num) if "volume" in df.columns else None,
        "turnover": df["exchange"].map(to_num) if "exchange" in df.columns else None,
    })
    out_df = out_df.iloc[::-1].reset_index(drop=True)   # 倒序 → 升序
    out_df = out_df.dropna(subset=["close"]).reset_index(drop=True)
    out_df.to_csv(cache, index=False)
    return out_df


def load_fundamentals(code: str) -> list[dict[str, Any]]:
    """取财报（利润表），返回按**发布日升序**的记录：用于防未来函数的同比计算。"""
    import json

    cache = CACHE_DIR / f"fin_{code}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 异常，按空数据继续: %s", "load_fundamentals", exc)
            cache.unlink(missing_ok=True)
    raw = run_cli(["finance", code, "--num", "12"])
    df = parse_md_table(raw)
    rows: list[dict[str, Any]] = []
    if not df.empty and "EndDate" in df.columns and "InfoPublDate" in df.columns:
        for _, r in df.iterrows():
            end = str(r.get("EndDate") or "")[:10]
            pub = str(r.get("InfoPublDate") or "")[:10]
            if not end or not pub:
                continue
            rows.append({
                "end_date": end,
                "pub_date": pub,
                "revenue": to_num(r.get("OperatingRevenue")),
                "net_profit": to_num(r.get("NPParentCompanyOwners")),
            })
    rows.sort(key=lambda x: x["pub_date"])
    cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


# --------------------------------------------------------------------- 进程内 providers

async def fetch_bars(code: str, market: str, limit: int) -> tuple[list[Any], str]:
    """用生产 providers 拉日线（含熔断/限流/故障转移），返回 (bars, source)。"""
    from ..providers import registry

    bars, src = await registry().kline(code, market, limit)
    return list(bars or []), src


async def fetch_minutes(code: str, market: str, limit: int,
                        klt: int = 5) -> tuple[list[Any], str]:
    """分钟线（默认 5 分钟），用于构造真实盘中快照。返回 (bars, source)。"""
    from ..providers import registry

    bars, src = await registry().kline_min(code, market, limit, klt=klt)
    return list(bars or []), src


def normalize_codes(raw: str | list[str]) -> list[str]:
    """把用户输入的股票池文本归一化为 6 位代码列表。"""
    if isinstance(raw, list):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,，\s;；]+", str(raw or ""))
    codes: list[str] = []
    for it in items:
        s = it.strip()
        if not s:
            continue
        m = re.search(r"(\d{6})", s)
        if m:
            codes.append(m.group(1))
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def to_westock_symbol(code: str) -> str:
    """6 位代码 → westock CLI 需要的 `sh600000` / `sz000001` 格式。"""
    code = re.sub(r"\D", "", str(code))[-6:]
    if code.startswith(("60", "68", "51", "58", "11")):
        return f"sh{code}"
    if code.startswith(("00", "30", "12", "15", "16")):
        return f"sz{code}"
    if code.startswith(("8", "4", "9")):
        return f"bj{code}"
    return f"sh{code}"


# --------------------------------------------------------------------- 统计工具

def summarize_by_bucket(
    df: pd.DataFrame, col: str, order: list[str], days: tuple[int, ...] = FORWARD_DAYS
) -> pd.DataFrame:
    """按档位聚合前瞻收益：样本数 / 占比 / 各窗口均值·中位·胜率。"""
    rows = []
    total = len(df)
    for b in order:
        sub = df[df[col] == b]
        if sub.empty:
            continue
        row: dict[str, Any] = {
            "档位": b,
            "样本数": len(sub),
            "占比%": round(len(sub) / total * 100, 1) if total else 0.0,
        }
        for nd in days:
            key = f"fwd{nd}"
            if key not in sub.columns:
                continue
            vals = sub[key].dropna()
            if vals.empty:
                continue
            row[f"{nd}日均值%"] = round(float(vals.mean()), 3)
            row[f"{nd}日中位%"] = round(float(vals.median()), 3)
            row[f"{nd}日胜率%"] = round(float((vals > 0).mean() * 100), 1)
        rows.append(row)
    return pd.DataFrame(rows)


def event_stats(vals: pd.Series) -> dict[str, Any]:
    """事件级汇总（禁止伪造 Sharpe / 年化 / 最大回撤）。"""
    v = vals.dropna()
    if v.empty:
        return {
            "total_events": 0, "avg_return_pct": 0.0, "median_return_pct": 0.0,
            "win_rate_pct": 0.0, "best_event_pct": 0.0, "worst_event_pct": 0.0,
        }
    return {
        "total_events": int(len(v)),
        "avg_return_pct": round(float(v.mean()), 3),
        "median_return_pct": round(float(v.median()), 3),
        "win_rate_pct": round(float((v > 0).mean() * 100), 1),
        "best_event_pct": round(float(v.max()), 3),
        "worst_event_pct": round(float(v.min()), 3),
    }


async def gather_limited(coro_list: list[Any], concurrency: int = 4) -> list[Any]:
    """带并发上限的 gather，避免一次性打爆数据源。"""
    sem = asyncio.Semaphore(max(1, min(8, concurrency)))

    async def wrap(coro: Any) -> Any:
        async with sem:
            return await coro

    return list(await asyncio.gather(*(wrap(c) for c in coro_list)))
