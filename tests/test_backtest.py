"""回测模块单元测试：不触网，覆盖注册表 / 落盘 / 评分纯函数 / 渲染工具。"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from backend import backtest as bt
from backend.backtest import compare_strategy, engine, intraday_strategy, render, score_strategy
from backend.backtest.dashboard.render_dashboard import build_dashboard_data, render_dashboard


# ------------------------------------------------------------------ 注册表

def test_registry_complete():
    items = bt.list_strategies()
    ids = [s["id"] for s in items]
    assert ids == ["score_threshold", "intraday_signal", "intraday_compare"]
    for s in items:
        assert s["name"] and s["desc"]
        assert s["schema"], f"{s['id']} 缺参数 schema"
        for f in s["schema"]:
            assert "key" in f and "label" in f and "default" in f


def test_registry_get_unknown():
    from backend.backtest.registry import get
    assert get("nope") is None
    assert get("score_threshold") is not None


# ------------------------------------------------------------------ engine 纯函数

def test_normalize_codes():
    assert engine.normalize_codes("600000，000001 sz300750; 601179.SH") == \
        ["600000", "000001", "300750", "601179"]
    assert engine.normalize_codes(["sh600000", "600000"]) == ["600000"]


def test_to_westock_symbol():
    assert engine.to_westock_symbol("600000") == "sh600000"
    assert engine.to_westock_symbol("000001") == "sz000001"
    assert engine.to_westock_symbol("sh601179") == "sh601179"


def test_parse_md_table():
    md = "| a | b |\n|---|---|\n| 1 | 2.5 |\n| 3 | x |"
    df = engine.parse_md_table(md)
    assert list(df.columns) == ["a", "b"]
    assert engine.to_num(df.iloc[0]["b"]) == 2.5
    assert engine.to_num(df.iloc[1]["b"]) is None


def test_event_stats_and_summarize():
    df = pd.DataFrame({
        "档位": ["A", "A", "B"],
        "fwd5": [1.0, -1.0, 3.0],
        "fwd1": [0.5, 0.5, 0.0],
    })
    rows = engine.summarize_by_bucket(df, "档位", ["A", "B"])
    a = rows[rows["档位"] == "A"].iloc[0]
    assert a["样本数"] == 2
    assert a["5日均值%"] == 0.0
    assert a["5日胜率%"] == 50.0
    stats = engine.event_stats(df["fwd5"])
    assert stats["total_events"] == 3
    assert stats["best_event_pct"] == 3.0
    assert stats["worst_event_pct"] == -1.0


# ------------------------------------------------------------------ 评分策略纯函数

def _make_panel(n: int = 140) -> pd.DataFrame:
    import random

    random.seed(7)
    rows, close = [], 10.0
    for i in range(n):
        close = max(1.0, close * (1 + random.uniform(-0.02, 0.021)))
        rows.append({
            "date": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "open": close * 0.995, "high": close * 1.01,
            "low": close * 0.99, "close": close,
            "volume": 1e7 + i * 1e5, "turnover": 1.5,
        })
    return pd.DataFrame(rows)


def test_compute_tech_and_scores():
    d = score_strategy.compute_tech(_make_panel())
    for w in (5, 10, 20, 60):
        assert f"ma{w}" in d.columns
    # warmup 之后分数有限且可复现
    s = score_strategy.tech_score(d, 100)
    assert -120 <= s <= 120
    assert s == score_strategy.tech_score(d, 100)   # 纯函数可复现
    f = score_strategy.fundamental_score([], "2025-01-01")
    assert f == 0.0


def test_bucket_by_threshold_matches_production():
    th = {"add": 28.0, "hold": 5.0, "reduce": -22.0}
    assert score_strategy.bucket_by_threshold(30, th) == "加仓"
    assert score_strategy.bucket_by_threshold(10, th) == "观望"
    assert score_strategy.bucket_by_threshold(-10, th) == "减仓"
    assert score_strategy.bucket_by_threshold(-30, th) == "清仓"


# ------------------------------------------------------------------ 盘口策略纯函数

def test_signal_labels():
    labels = intraday_strategy.signal_labels("当日高位强势，且放量上攻")
    pairs = dict(labels)
    assert pairs.get("高位强势") is True
    assert pairs.get("放量上攻") is True
    assert intraday_strategy.signal_labels("无信号文本") == []


def test_calibrate():
    assert "样本不足" in intraday_strategy.calibrate(0.9, 0.5, 10)
    assert "有效" in intraday_strategy.calibrate(0.60, 0.50, 200)
    assert "反向" in intraday_strategy.calibrate(0.40, 0.50, 200)


# ------------------------------------------------------------------ 对照策略纯函数

def test_compare_dir_rows_and_cmp_rows():
    samples = [
        {"close_score": 5, "intra_score": 5, "next_ret": 1.0,
         "close_labels": [("高位强势", True)], "intra_labels": [("高位强势", True)]},
        {"close_score": -5, "intra_score": 3, "next_ret": -1.0,
         "close_labels": [("放量下挫", False)], "intra_labels": [("放量上攻", True)]},
    ] * 20
    dir_rows, same, flip = compare_strategy._dir_rows(samples)
    assert flip > 0
    assert isinstance(same, int)
    cmp_rows = compare_strategy._cmp_rows(samples)
    assert cmp_rows, "对照行不应为空"
    for r in cmp_rows:
        assert "收盘样本" in r and "盘中样本" in r


# ------------------------------------------------------------------ 落盘（用临时目录替换 RUNS_DIR）

def test_store_roundtrip(tmp_path, monkeypatch):
    from backend.backtest import store

    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(store, "INDEX_FILE", tmp_path / "index.json")

    meta = store.create("score_threshold", "测试策略", {"codes": "600000"})
    rid = meta["run_id"]
    assert meta["status"] == "queued"

    store.progress(rid, 0.5, "半程")
    assert store.get_meta(rid)["progress"] == 0.5

    csv = tmp_path / "t.csv"
    csv.write_text("symbol,pnl_pct\n600000,1.2\n", encoding="utf-8-sig")
    store.finish(rid, {
        "summary": {"total_events": 1, "win_rate_pct": 100.0},
        "full_summary": {"meta": {}, "summary": {"total_events": 1}},
        "tables": {"signals": [{"信号": "X"}]},
        "trades_csv": str(csv),
    })
    got = store.get_meta(rid)
    assert got["status"] == "done" and got["has_report"] is False

    res = store.load_result(rid)
    assert res["summary"]["total_events"] == 1          # 扁平 summary
    assert "summary" in res["full_summary"]              # 完整结构保留
    assert res["tables"]["signals"][0]["信号"] == "X"

    rows = store.load_trades(rid)
    assert rows[0]["symbol"] == "600000"

    assert bt.list_runs()[0]["run_id"] == rid
    assert bt.delete_run(rid) is True
    assert store.get_meta(rid) is None


def test_store_busy_cleanup(tmp_path, monkeypatch):
    """进程重启后残留的 running 记录会被标记失败。"""
    from backend.backtest import store

    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(store, "INDEX_FILE", tmp_path / "index.json")
    meta = store.create("intraday_signal", "测试", {})
    store.update(meta["run_id"], status="running")       # 模拟重启前的状态
    store._RUNS.clear()                                   # 模拟进程重启（内存态丢失）
    assert store.is_busy() is None                       # 扫描后清理并返回 None
    assert store.get_meta(meta["run_id"])["status"] == "failed"


# ------------------------------------------------------------------ 看板渲染（离线）

def test_render_report_offline(tmp_path):
    trades = pd.DataFrame({
        "symbol": ["sh600000"] * 8,
        "signal_date": [f"2025-01-{i:02d}" for i in range(1, 9)],
        "entry_date": [f"2025-01-{i:02d}" for i in range(1, 9)],
        "exit_date": [f"2025-01-{i:02d}" for i in range(1, 9)],
        "entry_price": [10.0] * 8,
        "score": [30, 10, -10, -30, 5, 0, -5, 15],
        "档位_阈值": ["加仓", "观望", "减仓", "清仓", "观望", "观望", "减仓", "观望"],
        "档位_分位": ["Q4 高分", "Q3", "Q2", "Q1 低分"] * 2,
        "pnl_pct": [1.0, -0.5, 0.3, -1.0, 0.2, 0.0, -0.3, 0.8],
        "holding_days": [5] * 8,
        "label": ["x"] * 8,
    })
    csv = tmp_path / "trades.csv"
    trades.to_csv(csv, index=False, encoding="utf-8-sig")

    buckets = pd.DataFrame([
        {"档位": "加仓", "样本数": 1, "占比%": 12.5, "5日均值%": 1.0,
         "5日中位%": 1.0, "5日胜率%": 100.0, "10日胜率%": 100.0},
        {"档位": "清仓", "样本数": 1, "占比%": 12.5, "5日均值%": -1.0,
         "5日中位%": -1.0, "5日胜率%": 0.0, "10日胜率%": 0.0},
    ])
    out = tmp_path / "report.html"
    render.render_report(
        output_path=out,
        trades_csv=csv,
        meta={"strategy_name": "测试策略", "symbol": "测试", "start": "2025-01",
              "end": "2025-01", "note": "单元测试"},
        summary={"total_events": 8, "avg_return_pct": 0.06, "median_return_pct": 0.1,
                 "win_rate_pct": 50.0, "best_event_pct": 1.0, "worst_event_pct": -1.0},
        extra_modules=[
            {"type": "text", "tab": "overview", "title": "结论", "text": "- 测试结论"},
            {"type": "custom_html", "tab": "overview", "title": "对比图", "width": "full",
             "html": render.svg_bar_chart(buckets.to_dict(orient="records"))},
        ],
        tabs=[{"id": "overview", "label": "阈值检验"},
              {"id": "robust", "label": "稳健性"},
              {"id": "trades", "label": "事件明细"}],
        trade_columns=[{"key": "symbol", "label": "标的"},
                       {"key": "档位_阈值", "label": "档位", "format": "pill"},
                       {"key": "pnl_pct", "label": "收益", "format": "pct"}],
        group_col="档位_阈值",
    )
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "__REPORT_DATA__" not in html                    # 占位符已全部替换
    for kw in ("阈值检验", "结论", "测试策略"):
        assert kw in html, f"看板缺少 {kw}"
    for bad in ("夏普", "Sharpe", "年化收益", "最大回撤"):
        assert bad not in html, f"事件研究看板不应出现 {bad}"
    assert out.stat().st_size > 10_000


def test_svg_bar_chart_a_share_colors():
    rows = [{"档位": "A", "5日均值%": 0.5, "5日胜率%": 55.0},
            {"档位": "B", "5日均值%": -0.5, "5日胜率%": 45.0}]
    svg = render.svg_bar_chart(rows)
    assert 'fill="#e5534b"' in svg   # 涨=红
    assert 'fill="#2ea86b"' in svg   # 跌=绿


def test_prune_run_dirs(tmp_path, monkeypatch):
    """bt_* 过期运行目录应被清理；新鲜目录与 kline/fin 缓存文件不受影响。"""
    import os
    import time as _time

    old = tmp_path / "bt_score_old"
    fresh = tmp_path / "bt_intraday_new"
    keeper = tmp_path / "kline_sh600000_400.csv"
    old.mkdir()
    fresh.mkdir()
    keeper.write_text("date,close\n2026-01-01,9.0\n", encoding="utf-8")
    past = _time.time() - 8 * 86400
    os.utime(old, (past, past))
    monkeypatch.setattr(engine, "CACHE_DIR", tmp_path)

    removed = engine.prune_run_dirs(max_age_days=7.0)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert keeper.exists()
