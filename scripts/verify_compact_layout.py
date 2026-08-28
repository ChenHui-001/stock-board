"""临时脚本：截图验证 page-detail 的 compact 横向布局。

打开一个真实存在的股票详情页(用 000001.SZ 平安银行),
等待 renderStatus 渲染完成,定位到「区间与位置」section,
截图整张页面 + 单独 section,保存到 tmp。
"""
import os
import sys
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8765")
CODE = sys.argv[1] if len(sys.argv) > 1 else "000001.SZ"
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "tmp")
os.makedirs(OUT_DIR, exist_ok=True)


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto(f"{BASE}/#/stock/{CODE}", wait_until="domcontentloaded")
        page.wait_for_selector(".detail-head", timeout=15000)
        page.wait_for_function(
            "document.querySelector('#section-status') && "
            "document.querySelector('#section-status .stat-group.mode-compact')",
            timeout=20000,
        )
        # 等价格/数据稳定
        page.wait_for_timeout(800)
        full = os.path.join(OUT_DIR, "detail_full.png")
        sec = page.locator("#section-status")
        page.screenshot(path=full, full_page=True)
        sec.screenshot(path=os.path.join(OUT_DIR, "detail_section_status.png"))
        # 拿 section 内的几个统计块的 box
        boxes = page.evaluate(
            "() => {"
            "  const sec = document.getElementById('section-status');"
            "  if (!sec) return null;"
            "  const groups = sec.querySelectorAll('.stat-group.mode-compact');"
            "  return Array.from(groups).map(g => {"
            "    const r = g.getBoundingClientRect();"
            "    return {top: r.top, height: r.height, items: g.querySelectorAll('.stat-compact').length,"
            "            firstLabel: g.querySelector('.stat-compact-label')?.textContent || '',"
            "            firstValue: g.querySelector('.stat-compact-value')?.textContent || '',"
            "            firstTitle: g.querySelector('.stat-compact-value')?.getAttribute('title') || ''};"
            "  });"
            "}"
        )
        print("STATUS_GROUPS:", boxes)
        browser.close()


if __name__ == "__main__":
    run()

