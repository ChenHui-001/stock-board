"""端到端 UI 验证：驱动真实浏览器走一遍三大页面 + AI 弹窗，截图留证。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8899"
SHOTS = Path(__file__).resolve().parent / "shots"
SHOTS.mkdir(exist_ok=True)

errors: list[str] = []


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 950})

        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        # ---------------- 首页 ----------------
        await page.goto(f"{BASE}/#/home", wait_until="networkidle")
        await page.wait_for_timeout(2500)
        rows = await page.locator(".wl-row:not(.wl-head)").count()
        print(f"[首页] 自选股行数 = {rows}")
        badge = await page.locator("#session-badge").inner_text()
        sub = await page.locator("#topbar-sub").inner_text()
        print(f"[首页] 时段 = {badge}")
        print(f"[首页] 副栏 = {sub}")
        if rows:
            first = page.locator(".wl-row:not(.wl-head)").first
            print("[首页] 首行 =", (await first.inner_text()).replace("\n", " | "))
        await page.screenshot(path=str(SHOTS / "1-home.png"), full_page=True)

        # 排序按钮
        await page.get_by_role("button", name="涨跌幅").click()
        await page.wait_for_timeout(400)
        print("[首页] 涨跌幅排序 OK")

        # 管理模式
        await page.locator("#btn-manage").click()
        await page.wait_for_timeout(400)
        boxes = await page.locator(".wl-row .checkbox").count()
        print(f"[首页] 管理模式复选框 = {boxes}")
        await page.screenshot(path=str(SHOTS / "2-home-manage.png"), full_page=True)
        await page.get_by_role("button", name="完成").click()
        await page.wait_for_timeout(300)

        # ---------------- 查询页 ----------------
        await page.goto(f"{BASE}/#/search", wait_until="networkidle")
        await page.wait_for_timeout(1200)
        await page.fill("#search-input", "pfyh")
        await page.wait_for_timeout(2200)
        results = await page.locator(".result-row").count()
        print(f"[查询] 搜索 pfyh 结果 = {results}")
        if results:
            print("[查询] 首条 =", (await page.locator(".result-row").first.inner_text()).replace("\n", " | "))
        hot_rows = await page.locator(".hot-row").count()
        print(f"[查询] 热门榜条目 = {hot_rows}")
        await page.screenshot(path=str(SHOTS / "3-search.png"), full_page=True)

        # 中文搜索
        await page.fill("#search-input", "宁德")
        await page.wait_for_timeout(2200)
        print(f"[查询] 搜索 宁德 结果 = {await page.locator('.result-row').count()}")

        # ---------------- 详情页 ----------------
        await page.goto(f"{BASE}/#/stock/600000", wait_until="networkidle")
        await page.wait_for_timeout(4000)
        name = await page.locator(".detail-name").inner_text()
        price = await page.locator(".detail-price").inner_text()
        change = await page.locator(".detail-change").inner_text()
        print(f"[详情] {name} 现价={price} {change}")
        ma_cards = await page.locator(".ma-card").count()
        tags = await page.locator(".status-tag").count()
        quote_cells = await page.locator(".quote-cell").count()
        print(f"[详情] 均线卡片={ma_cards} 状态标签={tags} 基础行情格={quote_cells}")

        # 图表是否真的渲染出画布
        canvases = await page.locator(".chart canvas").count()
        print(f"[详情] 图表 canvas = {canvases}")
        for cid in ("chart-ma", "chart-flow", "chart-margin", "chart-margin-flow"):
            exists = await page.locator(f"#{cid}").count()
            has_canvas = await page.locator(f"#{cid} canvas").count() if exists else 0
            print(f"        {cid}: 存在={bool(exists)} canvas={has_canvas}")

        flow_rows = await page.locator(".data-table tbody tr").count()
        print(f"[详情] 数据表行数（资金+两融）= {flow_rows}")
        await page.screenshot(path=str(SHOTS / "4-detail.png"), full_page=True)

        # ---------------- AI 分析弹窗 ----------------
        await page.get_by_role("button", name="AI 分析").click()
        await page.wait_for_selector(".ai-verdict", timeout=60000)
        await page.wait_for_timeout(700)
        action = await page.locator(".ai-action").inner_text()
        reason = await page.locator(".ai-reason").inner_text()
        levels = await page.locator(".ai-level").count()
        sections = await page.locator(".ai-section-title").count()
        print(f"[AI] 结论 = {action}")
        print(f"[AI] 依据 = {reason[:90]}…")
        print(f"[AI] 价位格 = {levels}  分析模块 = {sections}")
        await page.screenshot(path=str(SHOTS / "5-ai.png"), full_page=True)

        # 复制按钮
        await page.get_by_role("button", name="复制").click()
        await page.wait_for_timeout(600)

        # 关闭（限定 AI 弹窗作用域，避免匹配到设置弹窗的关闭按钮）
        await page.locator("#modal-root .modal-close").click()
        await page.wait_for_timeout(300)
        hidden = await page.locator("#modal-root").is_hidden()
        print(f"[AI] 弹窗已关闭 = {hidden}")

        # ---------------- 窄屏自适应 ----------------
        await page.set_viewport_size({"width": 430, "height": 900})
        await page.goto(f"{BASE}/#/home", wait_until="networkidle")
        await page.wait_for_timeout(2200)
        overflow = await page.evaluate(
            "() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 2"
        )
        print(f"[窄屏 430px] 横向溢出 = {overflow}")
        await page.screenshot(path=str(SHOTS / "6-mobile.png"), full_page=True)

        await browser.close()

    print("\n=== 浏览器错误 ===")
    if errors:
        for e in errors:
            print("  ", e)
    else:
        print("   无")
    print(f"\n截图目录: {SHOTS}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
