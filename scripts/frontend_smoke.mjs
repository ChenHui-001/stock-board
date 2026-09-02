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
  getElementById: (id) => el(id),
  querySelector: (s) => el('view').querySelector(s),
  querySelectorAll: () => [],
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
  if (url.indexOf('/api/hot') >= 0) { bump('hot'); return json({ gainers: [], losers: [], actives: [] }); }
  if (url.indexOf('/api/meta') >= 0) { bump('meta'); return json({ providers: [], ai: {}, session: null }); }
  if (url.indexOf('/api/value') >= 0) { bump('value'); return json({ rows: [] }); }
  if (url.indexOf('/api/hotspot') >= 0) { bump('hotspot'); return json({ boards: [] }); }
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

// ---- 详情页来回切：确认重新挂载后定时器能恢复（不是被 destroy 一棒子打死）
{
  await goto('#/stock/600000');
  const before = count('quote');
  await sleep(2600);
  check('详情页：重新挂载后倒计时恢复', count('quote') > before,
    'quote ' + before + ' → ' + count('quote'));
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
