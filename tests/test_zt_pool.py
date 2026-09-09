"""涨停池共享模块（zt_pool）：标签规则 + 代码映射 + 降级路径。"""
from __future__ import annotations

from tests._common import *  # noqa: F401,F403  公共导入见 tests/_common.py


def test_zt_label_rules() -> None:
    from backend.zt_pool import zt_label

    # 连板数 >= 2 → N连板
    assert zt_label(2, None, None) == "2连板"
    assert zt_label(3, 3, 3) == "3连板"
    # 连板 1 + 无历史板 → 首板
    assert zt_label(1, None, None) == "首板"
    assert zt_label(1, 1, 1) == "首板"
    # 连板 1 + days > ct → X天Y板（隔日断板再涨停）
    assert zt_label(1, 5, 3) == "5天3板"
    assert zt_label(1, 2, 1) == "2天1板"
    # 未涨停 / 异常值 → 空串（不渲染徽标）
    assert zt_label(0, None, None) == ""
    assert zt_label(None, 5, 3) == ""
    assert zt_label(-1, 5, 3) == ""


def test_match_zt_mapping() -> None:
    from backend.zt_pool import match_zt

    pool = {"count": 3, "rows": [
        {"code": "600127", "lianban": 3, "zttj_days": 3, "zttj_ct": 3,
         "board": "电网设备", "seal_amount": 123456789},
        {"code": "002579", "lianban": 1, "zttj_days": 5, "zttj_ct": 2,
         "board": "", "seal_amount": None},
        {"code": "bad", "lianban": 1},          # 非 6 位 → 跳过
        {"lianban": 1},                          # 缺 code → 跳过
    ]}
    zt_map = match_zt(pool)
    assert set(zt_map) == {"600127", "002579"}, str(zt_map)
    assert zt_map["600127"]["label"] == "3连板"
    assert zt_map["600127"]["board"] == "电网设备"
    assert zt_map["002579"]["label"] == "5天2板"
    # 空结构不炸
    assert match_zt(None) == {}
    assert match_zt({"count": 0, "rows": []}) == {}


def test_value_screener_delegates_to_shared_pool(monkeypatch) -> None:
    """value_screener._fetch_zt_pool 必须走共享缓存，不允许再私有取数。"""
    from backend import value_screener, zt_pool

    called = {}

    async def fake_get(force=False):
        called["force"] = force
        return {"count": 1, "rows": [{"code": "600127", "label": "首板"}]}

    monkeypatch.setattr(zt_pool, "get_zt_pool", fake_get)
    out = asyncio.run(value_screener._fetch_zt_pool())
    assert out == {"count": 1, "rows": [{"code": "600127", "label": "首板"}]}
    assert called == {"force": False}
