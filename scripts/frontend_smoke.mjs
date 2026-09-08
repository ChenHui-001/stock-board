/* 前端结构级冒烟（Node，无浏览器依赖）
 *
 * 目的：本项目前端零自动化测试，每次改 frontend/static/ 都只能靠肉眼。这里用
 * 假 DOM + 假 fetch 在 Node 里真实加载 app.js，守住三件最容易回归的事：
 *   1. 各页面模块能被 import（改坏 import / 语法错误会立刻炸）
 *   2. route() 在各路由之间切换不抛异常
 *   3. destroy 卸载链路真的被调用——页面里跨路由存活的定时器/轮询必须停
 *
 * 范围刻意收窄在「结构级」：不断言任何页面的具体渲染内容（详情页/回测页马上
 * 要大改，写业务断言等于白写）。只断言「能不能加载、切路由崩不崩、卸载生效没」。
 *
 * 运行：node scripts/frontend_smoke.mjs
 */
import { pathToFileURL } from 'node:url';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// FRONTEND_JS_DIR 可以指向另一份 js 目录（例如修复前的历史版本），
// 用来验证本冒烟确实能测出问题、而不是永远绿灯。
const JS_DIR = process.env.FRONTEND_JS_DIR
  ? path.resolve(process.env.FRONTEND_JS_DIR)
  : path.resolve(HERE, '..', 'frontend', 'static', 'js');
const APP_JS = path.join(JS_DIR, 'app.js');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------ 假 DOM
function makeClassList(node) {
  const get = () => String(node.className || '').split(/\s+/).filter(Boolean);
  const set = (arr) => { node.className = arr.join(' '); };
  return {
    add: (...c) => { const a = get(); c.forEach((x) => { if (a.indexOf(x) < 0) a.push(x); }); set(a); },
    remove: (...c) => set(get().filter((x) => c.indexOf(x) < 0)),
    contains: (c) => get().indexOf(c) >= 0,
    toggle: (c, on) => {
      const has = get().indexOf(c) >= 0;
      const want = on == null ? !has : !!on;
      if (want && !has) { const a = get(); a.push(c); set(a); }
      if (!want && has) set(get().filter((x) => x !== c));
    },
  };
}

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this._class = '';
    this.textContent = '';
    this.offsetWidth = 0;
    this.hidden = false;
    this.classList = makeClassList(this);
  }
  // id 走 attributes，让 getElementById / '#id' 选择器能找到直接赋值的 node.id
  get id() { return this.attributes.id || ''; }
  set id(v) { this.attributes.id = v; }
  get className() { return this._class; }
  set className(v) { this._class = String(v || ''); }
  set innerHTML(v) { this._innerHTML = v; if (v === '') this.childNodes.length = 0; }
  get innerHTML() { return this._innerHTML || ''; }
  appendChild(c) { this.childNodes.push(c); if (c) c.parentNode = this; return c; }
  remove() { }
  setAttribute(k, v) { this.attributes[k] = v; if (k === 'class') this._class = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  addEventListener() { }
  removeEventListener() { }
  scrollIntoView() { }
  focus() { }
  getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; }
  _all() { const out = []; const walk = (n) => (n.childNodes || []).forEach((c) => { out.push(c); walk(c); }); walk(this); return out; }
  _matches(term) {
    const parts = term.match(/[.#]?[\w-]+/g) || [];
    return parts.every((p) => {
      if (p.charAt(0) === '.') return this.classList.contains(p.slice(1));
      if (p.charAt(0) === '#') return this.attributes.id === p.slice(1);
      return this.tagName === p.toUpperCase();
    });
  }
  // 支持逗号分组 + 后代选择器（'.a .b'）。不做完整 CSS 解析，够用即可：
  // 末段必须匹配节点自身，其余段由近到远向上找祖先匹配（允许跨层，即后代语义）
  _matchChain(parts) {
    if (!this._matches(parts[parts.length - 1])) return false;
    let idx = parts.length - 2;
    let cur = this.parentNode;
    while (idx >= 0) {
      if (!cur) return false;
      if (cur._matches && cur._matches(parts[idx])) idx--;
      cur = cur.parentNode;
    }
    return true;
  }
  querySelectorAll(sel) {
    const groups = String(sel).split(',').map((s) => s.trim()).filter(Boolean);
    return this._all().filter((n) => n._matches && groups.some((g) => n._matchChain(g.split(/\s+/))));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

const ids = {};
const el = (id) => (ids[id] || (ids[id] = new El('div')));
el('toast-root');
el('view');

globalThis.document = {
  createElement: (t) => new El(t),
  createElementNS: (ns, t) => new El(t),
  createTextNode: (t) => ({ nodeType: 3, tagName: '#TEXT', textContent: String(t), childNodes: [] }),
  getElementById: (id) => {
    // 优先在 #view 树里找页面真正渲染出来的节点，找不到再按需造一个空壳。
    // 否则 getElementById 会返回一个游离节点，页面里依赖它的逻辑（如锚点
    // 观察器 observe 的 section）就测不到真实行为。
    return el('view').querySelector('#' + id) || el(id);
  },
  querySelector: (s) => el('view').querySelector(s),
  // 同样委托到 #view 树：nav 里那些 .nav-item 不在 #view 内，返回 [] 是对的
  querySelectorAll: (s) => el('view').querySelectorAll(s),
  addEventListener: () => { },
  body: new El('body'),
  readyState: 'complete',
  hidden: false,
};

// 记录 window 上注册的监听，用来真实触发 hashchange（走和浏览器一样的导航路径）
const winListeners = {};
globalThis.window = {
  addEventListener: (ev, cb) => { (winListeners[ev] = winListeners[ev] || []).push(cb); },
  removeEventListener: () => { },
  open: () => { },
  scrollTo: () => { },
  scrollY: 0,
  innerWidth: 1920,
  innerHeight: 1080,
};
globalThis.location = { hash: '#/search', reload: () => { } };
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
globalThis.cancelAnimationFrame = (h) => clearTimeout(h);
globalThis.echarts = {
  init: () => ({ setOption: () => { }, resize: () => { }, dispose: () => { }, on: () => { } }),
  getInstanceByDom: () => null,
  dispose: () => { },
};

// 假 IntersectionObserver：记录所有实例与 observe/disconnect 调用。
// 详情页的锚点高亮靠它，destroy() 有没有真的断开，只能从这里看出来。
const observers = [];
class FakeIntersectionObserver {
  constructor(cb, opts) {
    this.cb = cb;
    this.opts = opts;
    this.observed = [];
    this.disconnected = false;
    observers.push(this);
  }
  observe(node) { this.observed.push(node); }
  unobserve(node) { this.observed = this.observed.filter((n) => n !== node); }
  disconnect() { this.disconnected = true; this.observed = []; }
  takeRecords() { return []; }
}
globalThis.IntersectionObserver = FakeIntersectionObserver;
globalThis.window.IntersectionObserver = FakeIntersectionObserver;

// ------------------------------------------------------------------ 假 fetch
const reqs = {};
const bump = (k) => { reqs[k] = (reqs[k] || 0) + 1; };
const count = (k) => reqs[k] || 0;
const json = (o) => ({ ok: true, status: 200, text: async () => JSON.stringify(o) });

// 详情页的数据只需够 renderHead 走到 renderDataStamp（那里才会建倒计时定时器）；
// 后续区块渲染失败会被 load() 的 catch 兜住，不影响本冒烟要断言的东西。
const DETAIL = {
  quote: { code: '600000', name: '浦发银行', price: 10.5, change: 0.1, change_pct: 1.2, trade_date: '2026-09-01', status: 'normal' },
  session: { label: '交易中', trading: true, interval_ms: 1000, auto_refresh: true },
  kline: [],
  ma_summary: { series: [], last: {} },
  fund_flow: { summary: {}, rows: [] },
  margin: { summary: {}, rows: [] },
  financials: { rows: [], reports: [] },
  status: { trend: {} },
  support_resistance: {},
  sources: {},
  news: [],
};

globalThis.fetch = async (path) => {
  const url = String(path);
  if (url.indexOf('/api/stock/') >= 0) { bump('detail'); return json(DETAIL); }
  if (url.indexOf('/api/quote/') >= 0) { bump('quote'); return json({ quote: DETAIL.quote, session: DETAIL.session }); }
  if (url.indexOf('/api/backtest/strategies') >= 0) {
    bump('bt_strategies');
    // running 非空：让回测页一挂载就起轮询，下面的「切走后停止」断言才不是空转
    return json({
      strategies: [{ id: 's1', name: '策略甲', desc: 'd', kind: 'event_study', schema: [] }],
      running: 'r-smoke',
    });
  }
  if (url.indexOf('/api/backtest/runs?') >= 0) { bump('bt_runs'); return json({ runs: [] }); }
  if (/\/api\/backtest\/run\/[^/?]+$/.test(url)) { bump('bt_status'); return json({ status: 'running', progress: 0.3, stage: '取数中' }); }
  if (url.indexOf('/api/watchlist') >= 0) { bump('watchlist'); return json({ rows: [], updated_at: '' }); }
  // 注意顺序：/api/hotspot 必须在 /api/hot 之前匹配（否则被 '/api/hot' 前缀吞掉）
  if (url.indexOf('/api/hotspot/analyze') >= 0) { bump('hotspot_analyze'); return json({ ok: false, error: 'smoke' }); }
  if (url.indexOf('/api/hotspot') >= 0) {
    bump('hotspot');
    // 发酵强度模型 payload：sector_heat 带 heat/heat_norm/slope/trend 三档 +
    // 一条旧缓存条目（无 heat 字段，验证前端 ?? 兜底）；leaders 供下钻面板消费
    const now = Math.floor(Date.now() / 1000);
    return json({
      items: [
        { id: 'h1', title: '银行板块逆市走强', summary: '主力资金持续净流入', ts: now - 60,
          time: 'x 10:00:00', source: '财联社', origin: '财联社', media_badge: true, dups: 2, url: '',
          tags: [{ name: '银行', sentiment: '利好', score: 3, hit: '银行', src: 'title' }] },
        { id: 'h2', title: '光伏产业链价格企稳', summary: '组件排产回升', ts: now - 300,
          time: 'x 09:56:00', source: '同花顺', origin: '同花顺', media_badge: false, dups: 0, url: '',
          tags: [{ name: '光伏', sentiment: '中性', score: 1, hit: '光伏', src: 'summary' }] },
        { id: 'h3', title: '券商板块冲高回落', summary: '尾盘跳水', ts: now - 2000,
          time: 'x 09:30:00', source: '新浪财经', origin: '新浪财经', media_badge: false, dups: 0, url: '',
          tags: [{ name: '券商', sentiment: '利空', score: 3, hit: '券商', src: 'title' }] },
      ],
      meta: {
        window_minutes: 30, total: 3, fetched_at: '2026-01-01 10:00:00', sources: [],
        sector_heat: [
          { name: '银行', total: 3, bull: 2, bear: 0, neutral: 1, trend: 'up',
            recent: 3, older: 0, heat: 6.2, heat_norm: 100, slope: 0.8, rank_score: 150 },
          { name: '光伏', total: 2, bull: 0, bear: 0, neutral: 2, trend: 'flat',
            recent: 1, older: 1, heat: 3.4, heat_norm: 55, slope: 0.02, rank_score: 55.6 },
          { name: '券商', total: 1, bull: 0, bear: 1, neutral: 0, trend: 'down',
            recent: 0, older: 1, heat: 1.9, heat_norm: 30, slope: -0.5, rank_score: 22.5 },
          { name: '旧概念', total: 1, bull: 0, bear: 0, neutral: 1, trend: 'flat',
            recent: 0, older: 1 },   // 旧缓存条目：无 heat/heat_norm/slope 字段
        ],
        leaders: [
          { board: '银行', code: 'BK0475', name: '银行', pct_chg: 1.2, main_net: 5.6e8,
            leader_name: '工商银行', leader_code: '601398', leader_pct: 2.1 },
          { board: '光伏设备', code: 'BK1036', name: '光伏设备', pct_chg: -0.4, main_net: -1.2e8,
            leader_name: '隆基绿能', leader_code: '601012', leader_pct: -1.1 },
        ],
      },
    });
  }
  if (url.indexOf('/api/hot') >= 0) { bump('hot'); return json({ gainers: [], losers: [], actives: [] }); }
  if (url.indexOf('/api/meta') >= 0) { bump('meta'); return json({ providers: [], ai: {}, session: null }); }
  if (url.indexOf('/api/value') >= 0) { bump('value'); return json({ rows: [] }); }
  return json({});
};

// ------------------------------------------------------------------ 断言
const results = [];
function check(name, ok, extra) {
  results.push({ name, ok: !!ok, extra: extra == null ? '' : String(extra) });
}

// ------------------------------------------------------------------ 跑起来
let App;
try {
  // app.js 顶层会直接 App.start()，所以 import 之前必须把 location.hash 设好。
  // 起点选 search：它最轻，不依赖行情数据，import 期的第一次 route() 不会假摔。
  ({ App } = await import(pathToFileURL(APP_JS).href));
  check('app.js 能被 import（含全部页面模块）', true);
} catch (e) {
  check('app.js 能被 import（含全部页面模块）', false, e && e.stack ? e.stack.split('\n')[0] : e);
  report();
}

// 页面模块导出面：mount/refresh/tick 必须都在，否则 app.js 调用时会炸
{
  const mods = [
    ['PageHome', 'page-home.js'], ['PageSearch', 'page-search.js'],
    ['PageValue', 'page-value.js'], ['PageHotspot', 'page-hotspot.js'],
    ['PageDetail', 'page-detail.js'], ['PageBacktest', 'page-backtest.js'],
  ];
  for (const [name, file] of mods) {
    const mod = await import(pathToFileURL(path.join(JS_DIR, file)).href).catch((e) => ({ __err: e }));
    const M = mod[name];
    const ok = M && typeof M.mount === 'function' && typeof M.refresh === 'function' && typeof M.tick === 'function';
    check('页面模块导出完整 mount/refresh/tick：' + name, ok,
      M ? '' : ((mod.__err && mod.__err.message) || '模块未导出 ' + name));
  }
}

// 真实导航：改 hash 后触发 app.js 注册的 hashchange 监听（和浏览器行为一致）
async function goto(hash) {
  globalThis.location.hash = hash;
  const cbs = winListeners['hashchange'] || [];
  if (!cbs.length) throw new Error('app.js 没有注册 hashchange 监听，冒烟无法驱动路由');
  await Promise.all(cbs.map((cb) => cb()));
  await sleep(120);   // 给页面 mount 里的异步请求留一点时间
}

const ROUTES = [
  ['#/search', '查询页'],
  ['#/value', '价值投资'],
  ['#/hotspot', '热点追踪'],
  ['#/backtest', '策略回测'],
  ['#/home', '首页'],
];
for (const [hash, label] of ROUTES) {
  try {
    await goto(hash);
    check('路由切换不抛异常：' + label, true);
  } catch (e) {
    check('路由切换不抛异常：' + label, false, (e && e.stack ? e.stack.split('\n').slice(0, 2).join(' / ') : e));
  }
}

// ---- 热点页：发酵强度模型 UI（三组分区 / 热度条 / fresh 三档 / dups / leaders 下钻）
{
  await goto('#/hotspot');
  await sleep(300);
  const view = el('view');
  if (process.env.DEBUG_HOTSPOT) {
    console.log('--- hotspot fetch count:', count('hotspot'));
    try {
      const ph = await import(pathToFileURL(path.join(JS_DIR, 'page-hotspot.js')).href);
      const data = await API.hotspot(30, false);
      console.log('--- direct API ok:', !!data, 'items=', (data.items || []).length,
        'heat=', ((data.meta || {}).sector_heat || []).length);
      await ph.PageHotspot.mount();
      console.log('--- remount ok');
    } catch (e) {
      console.log('--- remount threw:', e && e.stack);
    }
    const dump = (n, d) => {
      console.log('  '.repeat(d) + '<' + n.tagName + ' class=' + n.className + '> text=' + String(n.textContent).slice(0, 40));
      (n.childNodes || []).slice(0, 12).forEach((c) => { if (c.tagName) dump(c, d + 1); });
    };
    dump(view, 0);
  }

  // 1. chips 按 trend 三组分区（发酵中/持平/退潮）
  const labels = view.querySelectorAll('.sector-heat-group-label');
  const labelText = labels.map((n) => String(n.textContent));
  check('热点页：概念 chips 三组分区（发酵中/持平/退潮）',
    labels.length >= 3 && labelText.some((t) => t.indexOf('发酵中') >= 0)
      && labelText.some((t) => t.indexOf('持平') >= 0) && labelText.some((t) => t.indexOf('退潮') >= 0),
    labelText.join(' | '));

  // 2. 发酵组 chip 带 sector-trend-up
  const upChips = view.querySelectorAll('.sector-row-up .sector-chip');
  check('热点页：发酵组 chip 带 sector-trend-up',
    upChips.length > 0 && upChips.every((c) => c.classList.contains('sector-trend-up')),
    upChips.length + ' chips');

  // 3. 退潮组 chip 带 sector-trend-down
  const downChips = view.querySelectorAll('.sector-row-down .sector-chip');
  check('热点页：退潮组 chip 带 sector-trend-down',
    downChips.length > 0 && downChips.every((c) => c.classList.contains('sector-trend-down')),
    downChips.length + ' chips');

  // 4. 热度条宽度 = heat_norm（榜内最高热度应为 100%）
  const upBar = upChips.length ? upChips[0].querySelector('.sector-chip-heatbar') : null;
  check('热点页：热度条宽度 = heat_norm（最高热度 100%）',
    !!upBar && String(upBar.style.width) === '100%', upBar ? String(upBar.style.width) : '无热度条');

  // 5. slope 色阶 class：up 红 / down 绿
  const downBar = downChips.length ? downChips[0].querySelector('.sector-chip-heatbar') : null;
  check('热点页：slope 色阶 class（up 红 / down 绿）',
    !!upBar && upBar.classList.contains('sector-bar-up')
      && !!downBar && downBar.classList.contains('sector-bar-down'),
    (upBar ? upBar.className : '缺up') + ' / ' + (downBar ? downBar.className : '缺down'));

  // 6. 旧缓存兜底：缺 heat/heat_norm 字段的条目热度条仍有宽度（回退 total 占比）
  const allBars = view.querySelectorAll('.sector-chip-heatbar');
  check('热点页：旧缓存（缺 heat 字段）热度条兜底渲染',
    allBars.length >= 4 && allBars.every((b) => /%$/.test(String(b.style.width))),
    allBars.map((b) => b.style.width).join(','));

  // 7. fresh 三档：≤2min 亮 / ≤10min 中 / 更早 暗
  const freshItems = view.querySelectorAll('.hotspot-item-fresh');
  const midItems = view.querySelectorAll('.hotspot-item-mid');
  check('热点页：fresh 三档（≤2min 亮 / ≤10min 中 / 更早暗）',
    freshItems.length >= 1 && midItems.length >= 1,
    'fresh=' + freshItems.length + ' mid=' + midItems.length);

  // 8. 跨源去重 dups 徽标
  check('热点页：跨源去重 dups 徽标渲染',
    view.querySelectorAll('.hotspot-dups').length >= 1,
    String(view.querySelectorAll('.hotspot-dups').length));

  // 9. 下钻面板：点击匹配 leaders 的 chip（发酵组唯一 chip = 银行）→ 面板含领涨股
  if (upChips.length && typeof upChips[0].onclick === 'function') {
    upChips[0].onclick();
    const panel = view.querySelector('.sector-leaders');
    const rows = view.querySelectorAll('.sector-leader-row');
    // 假 DOM 的 textContent 不聚合子节点，这里手工拼接行内文本
    const rowText = rows.map((r) => [r, ...(r.childNodes || [])]
      .map((n) => String(n.textContent || '')).join('')).join(' ');
    check('热点页：选中 chip 后 leaders 下钻面板渲染（含领涨股）',
      !!panel && rows.length >= 1 && rowText.indexOf('工商银行') >= 0,
      panel ? '行文本: ' + rowText.slice(0, 60) : '面板未渲染');
    upChips[0].onclick();   // 复位（取消选中）
  } else {
    check('热点页：选中 chip 后 leaders 下钻面板渲染（含领涨股）', false, '未找到可点击 chip');
  }

  // 10. 无匹配 leaders 的 chip（券商）→ 下钻入口隐藏
  if (downChips.length && typeof downChips[0].onclick === 'function') {
    downChips[0].onclick();
    check('热点页：无匹配 leaders 的 chip 不显示下钻面板',
      !view.querySelector('.sector-leaders'),
      view.querySelector('.sector-leaders') ? '面板意外渲染' : '');
    downChips[0].onclick(); // 复位
  } else {
    check('热点页：无匹配 leaders 的 chip 不显示下钻面板', false, '未找到退潮组 chip');
  }

  await goto('#/search');
}

// ---- 卸载链路：回测页（定时器由 startPolling 持有，跑起来就不自停）
{
  await goto('#/backtest');
  await sleep(2000);                 // 轮询周期 1.5s，此时应已发出若干次状态请求
  const polling = count('bt_status');
  check('回测页：挂载后轮询确实在跑（否则下面的断言是空转）', polling > 0,
    'status 请求 ' + polling + ' 次');
  try {
    await goto('#/search');          // 切走 → app.js 应调用 PageBacktest.destroy()
    const atLeave = count('bt_status');
    await sleep(3600);               // 轮询周期 1.5s，够再跑 2 轮
    check('回测页：切走后轮询停止', count('bt_status') === atLeave,
      '切走后又发了 ' + (count('bt_status') - atLeave) + ' 次状态请求');
  } catch (e) {
    check('回测页：切走后轮询停止', false, e.message);
  }
}

// ---- 卸载链路：详情页倒计时定时器（每 5s 打一次 /api/quote，离开后必须停）
{
  await goto('#/stock/600000');
  await sleep(2600);                 // interval_ms=1000，此时应已 tick 若干次
  const ticking = count('quote');
  check('详情页：挂载后倒计时定时器确实在跑（否则下面的断言是空转）', ticking > 0,
    'quote 请求 ' + ticking + ' 次');
  const atLeave = count('quote');
  await goto('#/search');            // 切走 → app.js 应调用 PageDetail.destroy()
  await sleep(3600);
  check('详情页：切走后定时器停止（不再打行情接口）', count('quote') === atLeave,
    '切走后又打了 ' + (count('quote') - atLeave) + ' 次 /api/quote');

  // 全局刷新定时器也会调 currentPage().tick()，未挂载的页面必须自己挡住
  const t = count('quote');
  await sleep(1600);
  check('详情页：切走后 tick() 不再发请求', count('quote') === t,
    '又打了 ' + (count('quote') - t) + ' 次');
}

// ---- 卸载链路：详情页锚点观察器（不发请求，但持续持有已摘除的 DOM 引用与闭包）
{
  await goto('#/stock/600000');
  await sleep(400);                  // setupAnchorObserver 是 setTimeout(...,100) 触发的
  const first = observers[observers.length - 1];
  check('详情页：挂载后创建了锚点观察器并 observe 了 section',
    !!first && first.observed.length > 0,
    first ? ('observe ' + first.observed.length + ' 个节点') : '没有创建观察器');

  // 行为级验证：真的触发一次「进入 ma 区块」，看锚点高亮会不会亮
  const maSec = first && first.observed.filter((n) => n.dataset && n.dataset.anchor === 'ma')[0];
  if (maSec) {
    first.cb([{ target: maSec, isIntersecting: true }]);
    const links = el('view').querySelectorAll('#detail-anchor .detail-anchor-item');
    const hit = links.filter((a) => a.dataset.target === 'ma')[0];
    check('详情页：锚点高亮生效（观察器回调能点亮对应 nav）',
      !!hit && hit.classList.contains('active'),
      hit ? ('class=' + hit.className) : '没找到 target=ma 的 nav（navLinks ' + links.length + ' 个）');
  } else {
    check('详情页：锚点高亮生效（观察器回调能点亮对应 nav）', false, '观察器没有观察到带 dataset.anchor 的 section');
  }

  await goto('#/search');            // 切走 → destroy() 必须 disconnect
  check('详情页：切走后锚点观察器已 disconnect', !!first && first.disconnected,
    first && first.disconnected ? '' : 'observer 仍持有 ' + (first ? first.observed.length : '?') + ' 个节点');

  // 重建路径：destroy 之后再进详情页，观察器要能重新建起来（不是被一棒子打死）
  await goto('#/stock/600000');
  await sleep(400);
  const rebuilt = observers[observers.length - 1];
  check('详情页：destroy 后重新挂载能重建观察器', rebuilt && rebuilt !== first && rebuilt.observed.length > 0,
    rebuilt ? ('新实例 observe ' + rebuilt.observed.length + ' 个节点') : '没有重建');
  await goto('#/search');
}

// ---- 详情页来回切：确认重新挂载后倒计时定时器能恢复（不是被 destroy 一棒子打死）
{
  await goto('#/stock/600000');
  const before = count('quote');
  await sleep(2600);
  check('详情页：重新挂载后倒计时恢复', count('quote') > before,
    'quote ' + before + ' → ' + count('quote'));
  await goto('#/search');
}

// ---- 价值页：保存权重后必须自动触发选股重新加载（权重 → 指纹 → 缓存作废 → 重拉）
{
  await goto('#/value');
  await sleep(300);   // 等 loadWeights 填好滑块并绑定保存/重置按钮
  const saveBtn = el('view').querySelector('.val-weights-actions .btn-primary');
  check('价值页：权重面板与保存按钮已渲染', !!saveBtn,
    saveBtn ? '' : '找不到 .val-weights-actions .btn-primary');
  if (saveBtn && typeof saveBtn.onclick === 'function') {
    const n0 = count('value');
    await saveBtn.onclick();
    const delta = count('value') - n0;
    // 保存(POST /api/value/weights) + 重拉(GET /api/value/screen) 至少两次 /api/value 请求
    check('价值页：保存权重后自动触发选股重新加载', delta >= 2,
      'value 请求 +' + delta);
    const status = el('view').querySelector('.val-weights-status');
    check('价值页：重算完成后状态栏提示「已按新权重更新」',
      !!status && String(status.textContent).indexOf('已按新权重') >= 0,
      status ? String(status.textContent) : '状态栏不存在');
  }
  await goto('#/search');
}

report();

function report() {
  let bad = 0;
  results.forEach((r) => {
    if (!r.ok) bad++;
    console.log((r.ok ? 'PASS  ' : 'FAIL  ') + r.name + (r.extra ? '  [' + r.extra + ']' : ''));
  });
  console.log(bad ? ('\n>>> frontend_smoke: ' + bad + ' 项失败') : '\n>>> frontend_smoke: 全部通过（' + results.length + ' 项）');
  process.exit(bad ? 1 : 0);
}
