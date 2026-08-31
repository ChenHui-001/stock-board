const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('requestfailed', r => errors.push('requestfailed: ' + r.url() + ' ' + (r.failure()?.errorText || '')));
  const baseUrl = process.env.SMOKE_URL || 'http://127.0.0.1:18765';
  await page.goto(baseUrl + '/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1500);
  const checks = {
    title: await page.title(),
    hasTopbar: await page.locator('header.topbar').count(),
    hasNav: await page.locator('nav#nav').count(),
    navItems: await page.locator('nav#nav .nav-item').count(),
    sessionBadge: await page.locator('#session-badge').textContent().catch(() => null),
    echartsLoaded: await page.evaluate(() => typeof window.echarts !== 'undefined'),
    mainHasContent: await page.evaluate(() => document.querySelector('main#view').children.length > 0),
  };
  const shot = 'tests/_smoke_data/frontend_smoke.png';
  await page.screenshot({ path: shot, fullPage: false });
  process.stdout.write(JSON.stringify(checks, null, 2) + '\n');
  process.stdout.write('SCREENSHOT: ' + shot + '\n');
  process.stdout.write('ERRORS: ' + (errors.length ? errors.join('\n') : '(none)') + '\n');
  await browser.close();
  process.exit(errors.length ? 1 : 0);
})().catch(e => { process.stderr.write('FATAL ' + e.message + '\n'); process.exit(2); });
