"""Helpers。"""
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

def test_backtest_selftest() -> None:
    import subprocess
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            [_sys.executable, str(root / "backtest_intraday.py"), "--selftest"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        detail = (proc.stdout or "")[-200:] + (proc.stderr or "")[-200:]
    except subprocess.TimeoutExpired as exc:
        assert (False), f"超时: {exc}"
        return
    assert (proc.returncode == 0), detail

    # 盘中 vs 收盘对照实验脚本自测（不触网，合成日线+分钟线）
    try:
        proc2 = subprocess.run(
            [_sys.executable, str(root / "backtest_compare.py"), "--selftest"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        detail2 = (proc2.stdout or "")[-200:] + (proc2.stderr or "")[-200:]
    except subprocess.TimeoutExpired as exc:
        assert (False), f"超时: {exc}"
        return
    assert (proc2.returncode == 0), detail2





def test_check_sources_backtest_struct() -> None:
    from backend import check_sources

    # _backtest_advice 口径与回测脚本一致
    assert (check_sources._backtest_advice(0.60, 0.46, 200) == "有效，可维持或上调权重")
    assert (check_sources._backtest_advice(0.49, 0.46, 200) == "有效，权重可维持")
    assert (check_sources._backtest_advice(0.44, 0.46, 200) == "偏弱，建议下调权重")
    assert (check_sources._backtest_advice(0.38, 0.46, 200) == "反向/无效，建议大幅下调或检查方向")
    assert (check_sources._backtest_advice(0.90, 0.46, 10) == "样本不足，暂不调整")

    # check_backtest 失败分支（空样本）返回 ok=False 且不抛错
    async def _probe_empty() -> dict:
        return await check_sources.check_backtest([("600000", "SH")], days=5)

    res = asyncio.run(_probe_empty())
    assert (res.get("ok") is False), str(res)

    # 回测脚本缺失降级分支：镜像未打包 backtest_intraday.py 时返回 degraded
    # 提示而非抛异常（容器运行时场景，不触网）
    orig_import = check_sources.__dict__.get("__bt_import_guard")
    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "backtest_intraday":
            raise ModuleNotFoundError("No module named 'backtest_intraday'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _fake_import
    try:
        res_d = asyncio.run(check_sources.check_backtest([("600000", "SH")], days=5))
    finally:
        builtins.__import__ = real_import
    assert (res_d.get("ok") is False and res_d.get("degraded") is True and "未打包" in res_d.get("error", "")), str(res_d)

    # 渲染函数能处理含回测段/无回测段的报告
    text_with = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": ["tencent"],
        "latest_trade_date": "2026-08-17", "issues": [],
        "backtest": {
            "ok": True, "samples": 100, "stocks": 1, "base_up_rate": 46.0,
            "base_avg_ret": 0.1, "buckets": [], "signals": [],
        },
    })
    assert ("盘口信号近期命中率" in text_with), text_with[:200]
    text_no = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": [],
        "latest_trade_date": "", "issues": [], "backtest": {"ok": False, "error": "回测失败: x"},
    })
    assert ("回测失败" in text_no)
    text_dg = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": ["tencent"],
        "latest_trade_date": "", "issues": [],
        "backtest": {"ok": False, "error": "回测脚本未打包进镜像（ModuleNotFoundError）", "degraded": True},
    })
    assert ("已降级" in text_dg and "其余自检正常" in text_dg), text_dg[:300]
    text_sk = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": ["tencent"],
        "latest_trade_date": "", "issues": [],
        "backtest": {"ok": False, "skipped": True, "error": "已跳过回测（仅数据源自检）"},
    })
    assert ("已跳过回测" in text_sk), text_sk[:300]
    # run_diagnostics 分离：with_backtest=False 时返回 skipped 且不执行回测
    report_fast = asyncio.run(check_sources.run_diagnostics("600000", with_backtest=False))
    assert (report_fast["backtest"].get("skipped") is True
          and report_fast["backtest_days"] == 0), str(report_fast["backtest"])[:120]
    # 回测深度参数化：with_backtest=True 时 backtest_days 传递并记录
    report_days = asyncio.run(check_sources.run_diagnostics("600000", with_backtest=True,
                                                             backtest_days=30))
    assert (report_days.get("backtest_days") == 30), str(report_days.get("backtest_days"))

    # 置信度分档：样本越深越可靠
    assert (check_sources._confidence(500)["level"] == "high"), str(check_sources._confidence(500))
    assert (check_sources._confidence(60)["level"] == "medium"), str(check_sources._confidence(60))
    assert (check_sources._confidence(10)["level"] == "low"), str(check_sources._confidence(10))
    # 汇总报告三层置信度字段（总体/分桶/信号）
    conf_samples = [
        {"score": 3, "next_ret": 1.2, "labels": [("高位强势", True)]},
        {"score": -2, "next_ret": -0.5, "labels": [("低位下跌", False)]},
        {"score": 0, "next_ret": 0.3, "labels": []},
    ] * 20
    conf_rep = check_sources._summarize_backtest_safe(
        {"samples": conf_samples, "per_stock": [{"code": "600000"}]}, [("600000", "SH")]
    )
    assert (conf_rep["confidence"]["level"] == "low"), str(conf_rep["confidence"])
    assert (all("confidence" in b for b in conf_rep["buckets"])), str(conf_rep["buckets"][:1])
    assert (all("confidence" in s for s in conf_rep["signals"])), str(conf_rep["signals"][:1])

    # 独立回测脚本同步置信度：confidence 分档与 render 报告含置信列（不触网）
    import backtest_intraday as _bt
    assert (_bt.confidence(500)[0] == "高" and _bt.confidence(60)[0] == "中"
          and _bt.confidence(10)[0] == "低"), str((_bt.confidence(500), _bt.confidence(60), _bt.confidence(10)))
    _bt_report = _bt.render({
        "per_stock": [{"code": "600000"}],
        "samples": [
            {"score": 3, "next_ret": 1.2, "labels": [("高位强势", True)]},
            {"score": -2, "next_ret": -0.5, "labels": [("低位下跌", False)]},
            {"score": 0, "next_ret": 0.3, "labels": []},
        ] * 40,
    })
    assert ("置信度:" in _bt_report and "置信" in _bt_report), _bt_report[:200]

    # _summarize_backtest_safe 兜底：异常样本结构不抛错，返回 ok=False
    bad_summary = check_sources._summarize_backtest_safe(
        {"samples": [{"score": 3} for _ in range(40)], "per_stock": []}, [("600000", "SH")]
    )
    assert (bad_summary.get("ok") is False and "回测统计失败" in bad_summary.get("error", "")), str(bad_summary)
    # 正常样本走通统计
    ok_samples = [
        {"score": 3, "next_ret": 1.2, "labels": [("高位强势", True)]},
        {"score": -2, "next_ret": -0.5, "labels": [("低位下跌", False)]},
        {"score": 0, "next_ret": 0.3, "labels": []},
    ] * 20
    good_summary = check_sources._summarize_backtest_safe(
        {"samples": ok_samples, "per_stock": [{"code": "600000"}]}, [("600000", "SH")]
    )
    assert (good_summary.get("ok") is True and good_summary["samples"] == 60), str(good_summary)[:200]


