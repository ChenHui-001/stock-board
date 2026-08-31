const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

  const errors = [];
  const logs = [];
  page.on('console', msg => {
    const type = msg.type();
    const text = msg.text();
    if (type === 'error') errors.push(text);
    else if (type === 'warning') logs.push(`[warn] ${text}`);
    else logs.push(`[${type}] ${text}`);
  });
  page.on('pageerror', err => errors.push(`pageerror: ${err.message}`));
  page.on('requestfailed', req => errors.push(`requestfailed: ${req.url()} ${req.failure()?.errorText}`));

  await page.goto('http://127.0.0.1:18765/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1500);

  const checks = {};
  checks.title = await page.title();
  checks.hasTopbar = await page.locator('header.topbar').count();
  checks.hasNav = await page.locator('nav#nav').count();
  checks.hasMain = await page.locator('main#view').count();
  checks.navItems = await page.locator('nav#nav .nav-item').count();
  checks.sessionBadge = await page.locator('#session-badge').textContent().catch(() => null);
  checks.echartsLoaded = await page.evaluate(() => typeof window.echarts !== 'undefined');
  checks.mainHasContent = await page.evaluate(() => document.querySelector('main#view').children.length > 0);

  const shot = 'tests/_smoke_data/frontend_smoke.png';
  await page.screenshot({ path: shot, fullPage: false });

  console.log(JSON.stringify(checks, null, 2));
  console.log('SCREENSHOT:', shot);
  console.log('CONSOLE_LOGS:', logs.slice(0, 20).join('\n'));
  console.log('ERRORS:', errors.length ? errors.join('\n') : '(none)');

  await browser.close();
  process.exit(errors.length ? 1 : 0);
})().catch(e => { console.error('FATAL', e); process.exit(2); });
