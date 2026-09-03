"""Regression test: dist 产物必须包含 echarts vendor 文件，但页面不得预载。

历史一：用户报告「股票详情页：图表库未加载，无法渲染曲线」。
根因之一：构建后 frontend/static/dist/vendor/echarts.min.js 缺失，导致
charts.js mount() 时 window.echarts 为 undefined，页面直接显示「图表库未加载」。

历史二（P2 #26 懒加载改造）：echarts 约 1MB 但全站只有详情页画图，
index.html 已移除预载 <script>，由 charts.js 在首个图表调用时按需注入。

本测试做三件事：
  1. 检查源 vendor 文件 frontend/static/public/vendor/echarts.min.js 存在（>= 500KB）
  2. 检查构建后 dist/vendor/echarts.min.js 存在且 SHA256 与源一致（vite publicDir 拷贝）
  3. 检查 index.html（源 + dist）不再预载 echarts，且 charts.js 保留懒加载注入逻辑

第 2、3 项的 dist 部分需要先跑 `npm run build`；如果跳过构建直接跑 pytest，
dist 相关断言会跳过（不会失败），保证本地 dev 流程不会被本测试阻断。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC_VENDOR = ROOT / "frontend" / "static" / "public" / "vendor" / "echarts.min.js"
DIST_VENDOR = ROOT / "frontend" / "static" / "dist" / "vendor" / "echarts.min.js"
DIST_INDEX = ROOT / "frontend" / "static" / "dist" / "index.html"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_source_vendor_present() -> None:
    """源 vendor 文件必须在，否则 vite publicDir 无东西可拷。"""
    assert SRC_VENDOR.exists(), f"missing source: {SRC_VENDOR}"
    size = SRC_VENDOR.stat().st_size
    assert size >= 500_000, (
        f"echarts.min.js suspiciously small ({size} bytes); "
        f"is the file actually echarts?"
    )


def test_dist_vendor_matches_source_after_build() -> None:
    """构建后 dist/vendor/echarts.min.js 必须存在且与源 SHA256 一致。"""
    if not DIST_INDEX.exists():
        # 没跑过 vite build（如 dev 模式直接跑 pytest）→ 跳过，不失败
        import pytest

        pytest.skip("dist/index.html 不存在；需先跑 `npm run build`")

    assert DIST_VENDOR.exists(), (
        f"dist 中缺少 echarts vendor: {DIST_VENDOR}。"
        "vite publicDir 未生效或 public/vendor/ 路径配置错位，"
        "Docker 镜像将无法加载图表。"
    )
    assert _sha256(DIST_VENDOR) == _sha256(SRC_VENDOR), (
        "dist/vendor/echarts.min.js 与源文件 SHA256 不一致；"
        "可能是手 COPY 旧文件绕过构建，或构建产物过期。"
    )


def test_dist_index_html_does_not_preload_echarts() -> None:
    """dist/index.html 不得再预载 echarts（P2 #26 懒加载改造后由 charts.js 按需注入）。"""
    if not DIST_INDEX.exists():
        import pytest

        pytest.skip("dist/index.html 不存在；需先跑 `npm run build`")

    html = DIST_INDEX.read_text(encoding="utf-8")
    matches = re.findall(
        r'<script[^>]*src=["\']([^"\']*echarts\.min\.js[^"\']*)["\']',
        html,
    )
    assert not matches, (
        f"dist/index.html 仍预载 echarts（{matches}）；"
        "懒加载改造后应删除该 <script>，由 charts.js 按需注入，"
        "否则非图表页面白背 1MB。"
    )


def test_source_index_html_does_not_preload_echarts() -> None:
    """源 frontend/index.html 不得预载 echarts；懒加载逻辑必须在 charts.js 内。"""
    src_index = ROOT / "frontend" / "index.html"
    assert src_index.exists()
    html = src_index.read_text(encoding="utf-8")
    assert "/vendor/echarts.min.js" not in html, (
        "源 frontend/index.html 仍预载 /vendor/echarts.min.js；"
        "懒加载改造后应由 charts.js 动态注入，非图表页面不应加载 1MB echarts。"
    )

    charts = ROOT / "frontend" / "static" / "js" / "charts.js"
    assert charts.exists()
    js = charts.read_text(encoding="utf-8")
    assert "/vendor/echarts.min.js" in js and "_loadEcharts" in js, (
        "charts.js 缺少懒加载注入逻辑（_loadEcharts + /vendor/echarts.min.js）；"
        "index.html 已不再预载，删掉注入逻辑会导致详情页永远「图表库未加载」。"
    )
