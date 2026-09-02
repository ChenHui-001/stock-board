"""Regression test: dist 产物必须包含 echarts vendor 文件，且 index.html 必须引用之。

历史：用户报告「股票详情页：图表库未加载，无法渲染曲线」。
根因之一：构建后 frontend/static/dist/vendor/echarts.min.js 缺失或 index.html
没有正确的 <script> 标签，导致 charts.js mount() 时 window.echarts 为 undefined，
页面直接显示「图表库未加载」。

本测试做三件事：
  1. 检查源 vendor 文件 frontend/static/public/vendor/echarts.min.js 存在（>= 500KB）
  2. 检查构建后 dist/vendor/echarts.min.js 存在且 SHA256 与源一致（vite publicDir 拷贝）
  3. 检查 dist/index.html 里的 <script src=...echarts.min.js> 路径包含 vendor 段

第 2、3 项需要先跑 `npm run build`；如果跳过构建直接跑 pytest，本测试会
跳过（不会失败），保证本地 dev 流程不会被本测试阻断。
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


def test_dist_index_html_references_echarts() -> None:
    """dist/index.html 必须有 echarts vendor 的 <script> 标签，路径含 vendor 段。"""
    if not DIST_INDEX.exists():
        import pytest

        pytest.skip("dist/index.html 不存在；需先跑 `npm run build`")

    html = DIST_INDEX.read_text(encoding="utf-8")
    # 必须有 echarts.min.js 的 script 引用
    matches = re.findall(
        r'<script[^>]*src=["\']([^"\']*echarts\.min\.js[^"\']*)["\']',
        html,
    )
    assert matches, (
        "dist/index.html 未引用 echarts.min.js；charts.js mount() 会判 window.echarts 为空。"
    )
    # 路径必须含 vendor/ 段，避免 base 配置导致 404
    for src in matches:
        assert "/vendor/" in src or "vendor/" in src, (
            f"echarts script 路径不含 vendor 段: {src}"
        )


def test_source_index_html_references_echarts_with_vendor_path() -> None:
    """源 frontend/index.html 也必须引用 vendor 路径，保证 dev 模式同样能加载。"""
    src_index = ROOT / "frontend" / "index.html"
    assert src_index.exists()
    html = src_index.read_text(encoding="utf-8")
    assert "/vendor/echarts.min.js" in html or "vendor/echarts.min.js" in html, (
        "源 frontend/index.html 未引用 /vendor/echarts.min.js；"
        "dev 模式 / Vite 都不会自动加载 echarts。"
    )
