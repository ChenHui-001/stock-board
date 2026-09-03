"""回归：/vendor/echarts.min.js 必须真实可达。

历史缺陷：charts.js 懒加载注入 "/vendor/echarts.min.js"，但后端只挂载了
/static（文件实际在 /static/public/vendor/ 或 /static/dist/vendor/），
根路径 /vendor 无人服务 → 404 → 详情页报「图表库未加载，无法渲染曲线」。
旧的 dist 测试只断言路径字符串存在于源码，未断言 URL 可解析——本测试
补上真正的可达性校验（起 TestClient 直接打该 URL）。
"""
from tests._common import *  # noqa: F401,F403


def test_vendor_echarts_url_resolvable():
    from fastapi.testclient import TestClient

    from backend import main

    client = TestClient(main.app)
    resp = client.get("/vendor/echarts.min.js")
    assert resp.status_code == 200, (
        f"/vendor/echarts.min.js HTTP {resp.status_code}；charts.js 懒加载会 404，"
        "详情页图表将显示「图表库未加载」。检查 main.py 的 /vendor 挂载。"
    )
    assert len(resp.content) >= 500_000, (
        f"echarts.min.js 体积异常（{len(resp.content)} 字节），疑似 404 页面或截断文件"
    )
