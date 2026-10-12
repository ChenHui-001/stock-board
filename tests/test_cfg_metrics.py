"""scorecfg / valuecfg / metrics 单元测试（体检 M6 收尾：补零测试模块）。

权重配置三件套（scorecfg/valuecfg）是纯 DB+环境变量逻辑，conftest 已做
DATA_DIR 隔离；metrics 是 prometheus_client 单例打点，只验证不抛 + 输出格式。
"""
from __future__ import annotations

from tests._common import *  # noqa: F401,F403  公共导入见 tests/_common.py


def test_scorecfg_clamp_and_fingerprint() -> None:
    """save 钳制到 [0.2, 3.0]；非法值回落默认；fingerprint 随权重变化、reset 恢复。"""
    import json

    from backend import scorecfg

    base = scorecfg.get_weights()
    assert set(base) == set(scorecfg.FIELDS), base

    # 越界钳制 + 非法值回落
    saved = scorecfg.save_weights({"tech": 99, "capital": "abc", "news": None})
    assert saved["tech"] == 3.0, saved                       # 上界
    assert saved["capital"] == round(base["capital"], 2), saved
    assert saved["news"] == round(base["news"], 2), saved

    fp_after = scorecfg.fingerprint()
    assert "tech:3.00" in fp_after, fp_after

    # save 后 DB 里确实是干净 JSON（必须在 reset 之前查——reset 会删 key）
    raw = scorecfg.storage.get_kv("score_weights")
    assert json.loads(raw)["tech"] == 3.0, raw

    # reset 回到环境变量基线，指纹恢复
    restored = scorecfg.reset_weights()
    assert restored == base, (restored, base)
    assert scorecfg.fingerprint() != fp_after or restored == base
    assert scorecfg.storage.get_kv("score_weights") is None


def test_valuecfg_dims_and_weights() -> None:
    """DIM_MAXES 与 BASE_TOTAL 自洽；save/reset/clamp 行为与 scorecfg 同构。"""
    from backend import valuecfg

    assert valuecfg.BASE_TOTAL == round(sum(valuecfg.DIM_MAXES.values()), 1) == 106.0
    assert set(valuecfg.FIELDS) == set(valuecfg.DIM_MAXES), valuecfg.FIELDS

    base = valuecfg.get_weights()
    assert set(base) == set(valuecfg.FIELDS), base

    saved = valuecfg.save_weights({k: 99 for k in valuecfg.FIELDS})
    assert all(v == 3.0 for v in saved.values()), saved

    restored = valuecfg.reset_weights()
    assert restored == base, (restored, base)


def test_metrics_observe_and_export() -> None:
    """打点函数不抛；export 输出 Prometheus 文本格式且含注册的指标族。"""
    from backend import metrics

    metrics.observe_duration("eastmoney", "success", 0.3)
    metrics.observe_duration("ths", "timeout", 6.5)
    metrics.update_source_gauge("eastmoney", consecutive_failures=2, circuit_open=False)
    metrics.update_source_gauge("ths", consecutive_failures=5, circuit_open=True)

    body, ctype = metrics.export()
    assert isinstance(body, bytes) and body, (type(body), len(body))
    assert "text/plain" in ctype, ctype
    text = body.decode("utf-8")
    for family in (
        "hotspot_source_requests_total",
        "hotspot_source_consecutive_failures",
        "hotspot_source_circuit_open",
        "hotspot_source_fetch_duration_seconds",
    ):
        assert family in text, family
    # Gauge 值确实被置上（ths 熔断中 = 1）
    assert 'hotspot_source_circuit_open{source="ths"} 1.0' in text, text
    assert 'hotspot_source_consecutive_failures{source="eastmoney"} 2.0' in text, text
