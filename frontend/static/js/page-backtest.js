/* page-backtest：策略回测页（选策略 → 配参数 → 提交 → 轮询进度 → 看结果/历史） */
import { U } from './util.js';
import { API } from './api.js';

let viewEl = null;

const state = {
  strategies: [],
  current: null,          // 选中的策略对象
  running: null,          // 进行中的 run_id
  pollTimer: null,
  runMeta: null,          // 最近一次运行的状态
  runs: [],               // 历史运行列表
  loading: false,
};

// 策略 id → 表格渲染顺序（哪个表先展示）
const TABLE_TITLES = {
  bucket_threshold: '阈值分档统计（信号后 1/3/5/10 日）',
  bucket_quantile: '分位数分档（检验评分本身是否有区分度）',
  yearly: '分年度稳定性（加仓档 vs 清仓档 5 日胜率差）',
  signals: '各信号命中率（命中 = 信号方向与次日方向一致）',
  buckets: '盘口总分分桶 vs 次日表现',
  per_stock: '各标的取数情况',
  direction: '两时点打分方向一致性',
  compare: '信号命中率对比（收盘 vs 盘中 14:00）',
};
const TABLE_ORDER = [
  'bucket_threshold', 'bucket_quantile', 'yearly',
  'signals', 'buckets', 'compare', 'direction', 'per_stock',
];

// KPI 卡：summary 里要展示哪些键（中文名 + 是否强调）
const KPI_LABELS = {
  total_events: ['事件/样本数', false],
  avg_return_pct: ['平均收益', true],
  median_return_pct: ['中位收益', true],
  win_rate_pct: ['胜率/上涨率', true],
  best_event_pct: ['最佳事件', false],
  worst_event_pct: ['最差事件', false],
  confidence: ['置信度', false],
  with_intraday: ['含盘中时点', false],
  baseline_up_rate_pct: ['基线上涨率', true],
};

// ------------------------------------------------------------------ 骨架

function skeleton() {
  viewEl.innerHTML = '';
  const wrap = U.el('div', 'page-backtest');
  wrap.appendChild(U.el('div', 'bt-head',
    '策略回测 · 用真实历史行情检验策略信号是否真的有效'));
  const tip = U.el('div', 'bt-tip',
    '事件研究口径：信号在当日收盘产生、次日执行，杜绝未来函数；不计手续费与滑点。' +
    '结果为统计检验，不构成投资建议。');
  wrap.appendChild(tip);

  const card = U.el('div', 'bt-panel');
  card.appendChild(U.el('div', 'bt-panel-title', '① 选择策略'));
  card.appendChild(U.el('div', 'bt-cards', U.NBSP));
  wrap.appendChild(card);

  const formCard = U.el('div', 'bt-panel');
  formCard.appendChild(U.el('div', 'bt-panel-title', '② 参数'));
  formCard.appendChild(U.el('div', 'bt-form', ''));
  const actions = U.el('div', 'bt-actions');
  const runBtn = U.el('button', 'btn btn-primary bt-run', '▶ 开始回测');
  runBtn.disabled = true;
  actions.appendChild(runBtn);
  actions.appendChild(U.el('span', 'bt-run-hint',
    '回测在后台执行（几十秒到几分钟），可离开本页，完成后回到此页查看结果'));
  formCard.appendChild(actions);
  formCard.appendChild(U.el('div', 'bt-progress-wrap'));
  wrap.appendChild(formCard);

  const resultCard = U.el('div', 'bt-panel bt-result-panel');
  resultCard.appendChild(U.el('div', 'bt-panel-title', '③ 运行结果'));
  resultCard.appendChild(U.el('div', 'bt-result', ''));
  wrap.appendChild(resultCard);

  const histCard = U.el('div', 'bt-panel');
  histCard.appendChild(U.el('div', 'bt-panel-title', '④ 历史运行'));
  histCard.appendChild(U.el('div', 'bt-history', ''));
  wrap.appendChild(histCard);

  viewEl.appendChild(wrap);
  return wrap;
}

// ------------------------------------------------------------------ 策略选择

function renderStrategies() {
  const box = viewEl.querySelector('.bt-cards');
  if (!box) return;
  box.innerHTML = '';
  if (!state.strategies.length) {
    box.appendChild(U.el('div', 'bt-empty', '策略清单加载失败'));
    return;
  }
  state.strategies.forEach(function (s) {
    const card = U.el('div', 'bt-card' + (state.current && state.current.id === s.id ? ' active' : ''));
    card.appendChild(U.el('div', 'bt-card-name', s.name));
    card.appendChild(U.el('div', 'bt-card-desc', s.desc));
    if (s.limits) {
      const lim = U.el('div', 'bt-card-limits', '⚠ ' + s.limits);
      card.appendChild(lim);
    }
    const kind = U.el('span', 'bt-kind', s.kind === 'event_study' ? '事件研究' : '策略回测');
    card.appendChild(kind);
    card.onclick = function () {
      if (state.running) { U.toast('有回测正在运行，等它完成再切换', 'warn'); return; }
      state.current = s;
      renderStrategies();
      renderForm();
    };
    box.appendChild(card);
  });
}

// ------------------------------------------------------------------ 参数表单

function renderForm() {
  const box = viewEl.querySelector('.bt-form');
  const runBtn = viewEl.querySelector('.bt-run');
  if (!box) return;
  box.innerHTML = '';
  if (!state.current) {
    runBtn.disabled = true;
    return;
  }
  runBtn.disabled = !!state.running;
  (state.current.schema || []).forEach(function (f) {
    const row = U.el('div', 'bt-field bt-field-' + f.type);
    const label = U.el('label', 'bt-label', f.label);
    label.htmlFor = 'bt-p-' + f.key;
    row.appendChild(label);
    let input;
    if (f.type === 'textarea') {
      input = document.createElement('textarea');
      input.rows = 3;
      input.value = f.default != null ? String(f.default) : '';
      input.placeholder = f.hint || '';
    } else {
      input = document.createElement('input');
      input.type = 'number';
      if (f.min != null) input.min = f.min;
      if (f.max != null) input.max = f.max;
      if (f.step != null) input.step = f.step;
      input.value = f.default != null ? f.default : '';
    }
    input.id = 'bt-p-' + f.key;
    input.dataset.key = f.key;
    row.appendChild(input);
    if (f.hint) row.appendChild(U.el('div', 'bt-field-hint', f.hint));
    box.appendChild(row);
  });
}

function collectParams() {
  const params = {};
  const box = viewEl.querySelector('.bt-form');
  if (!box) return params;
  box.querySelectorAll('input,textarea').forEach(function (input) {
    const key = input.dataset.key;
    if (!key) return;
    const v = String(input.value || '').trim();
    if (input.type === 'number') {
      const n = parseFloat(v);
      params[key] = isNaN(n) ? undefined : n;
    } else {
      params[key] = v;
    }
  });
  return params;
}

// ------------------------------------------------------------------ 提交 + 轮询

async function submitRun() {
  if (!state.current) return;
  const params = collectParams();
  const codes = String(params.codes || '').trim();
  if (!codes) { U.toast('请先填写股票池', 'warn'); return; }
  try {
    const meta = await API.backtestRun(state.current.id, params);
    state.running = meta.run_id;
    state.runMeta = meta;
    U.toast('回测已提交，后台执行中', 'ok');
    renderStrategies();
    renderForm();
    startPolling();
  } catch (e) {
    U.toast('提交失败：' + (e.message || e), 'warn');
  }
}

function startPolling() {
  stopPolling();
  const wrap = viewEl.querySelector('.bt-progress-wrap');
  if (wrap) {
    wrap.innerHTML = '';
    const bar = U.el('div', 'bt-progress');
    bar.appendChild(U.el('div', 'bt-progress-fill'));
    const text = U.el('div', 'bt-progress-text', '排队中…');
    wrap.appendChild(bar);
    wrap.appendChild(text);
  }
  state.pollTimer = setInterval(pollOnce, 1500);
  pollOnce();
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

async function pollOnce() {
  if (!state.running) { stopPolling(); return; }
  try {
    const meta = await API.backtestRunStatus(state.running);
    state.runMeta = meta;
    renderProgress(meta);
    if (meta.status === 'done') {
      stopPolling();
      state.running = null;
      renderStrategies();
      renderForm();
      renderResult(meta);
      loadHistory();
      U.toast('回测完成', 'ok');
    } else if (meta.status === 'failed') {
      stopPolling();
      state.running = null;
      renderStrategies();
      renderForm();
      const wrap = viewEl.querySelector('.bt-progress-wrap');
      if (wrap) {
        wrap.innerHTML = '';
        wrap.appendChild(U.el('div', 'bt-error', '✗ 回测失败：' + (meta.error || '未知错误')));
      }
      U.toast('回测失败', 'warn');
    }
  } catch (e) { /* 轮询失败静默重试 */ }
}

function renderProgress(meta) {
  const wrap = viewEl.querySelector('.bt-progress-wrap');
  if (!wrap) return;
  const fill = wrap.querySelector('.bt-progress-fill');
  const text = wrap.querySelector('.bt-progress-text');
  if (fill) fill.style.width = Math.round((meta.progress || 0) * 100) + '%';
  if (text) {
    text.textContent = (meta.stage || '运行中') +
      ' · ' + Math.round((meta.progress || 0) * 100) + '%';
  }
}

// ------------------------------------------------------------------ 结果渲染

function kpiCards(summary) {
  const grid = U.el('div', 'bt-kpis');
  Object.keys(KPI_LABELS).forEach(function (k) {
    if (!(k in summary)) return;
    const v = summary[k];
    const def = KPI_LABELS[k];
    const card = U.el('div', 'bt-kpi' + (def[1] ? ' hl' : ''));
    card.appendChild(U.el('div', 'bt-kpi-label', def[0]));
    let text = '—';
    if (typeof v === 'number') {
      text = (k.indexOf('pct') >= 0)
        ? (k === 'total_events' ? String(v) : (v > 0 ? '+' : '') + v.toFixed(v % 1 ? 3 : 1) + '%')
        : String(v);
    } else if (v != null) {
      text = String(v);
    }
    const val = U.el('div', 'bt-kpi-value' + (def[1] && typeof v === 'number' && k !== 'total_events'
      ? ' ' + (v > 0 ? 'up' : (v < 0 ? 'down' : 'flat')) : ''), text);
    card.appendChild(val);
    grid.appendChild(card);
  });
  return grid;
}

function toneFor(cell) {
  // metric 表格里带 raw 的数值单元格按相对基线上色（涨红跌绿）
  if (!cell || typeof cell.raw !== 'number') return '';
  return cell.raw > 0 ? 'up' : (cell.raw < 0 ? 'down' : '');
}

function renderTable(title, rows) {
  if (!rows || !rows.length) return null;
  const cols = Object.keys(rows[0]).filter(function (k) { return k.charAt(0) !== '_'; });
  const table = U.el('table', 'val-table bt-table');
  const thead = U.el('thead');
  const trh = U.el('tr');
  cols.forEach(function (c) { trh.appendChild(U.el('th', '', c)); });
  thead.appendChild(trh);
  table.appendChild(thead);
  const tbody = U.el('tbody');
  rows.forEach(function (r) {
    const tr = U.el('tr', 'val-row');
    cols.forEach(function (c, idx) {
      const cell = r[c];
      let text = cell;
      let cls = '';
      if (cell && typeof cell === 'object') {
        text = cell.main != null ? cell.main : '';
        cls = toneFor(cell);
      }
      const td = U.el('td', (idx === 0 ? 'bt-first-col ' : '') + cls, text == null ? '—' : String(text));
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  const wrap = U.el('div', 'bt-table-wrap');
  wrap.appendChild(U.el('div', 'bt-table-title', title));
  wrap.appendChild(table);
  return wrap;
}

function renderTrades(rows) {
  if (!rows || !rows.length) return null;
  const labels = {
    symbol: '标的', signal_date: '信号日', entry_date: '买入日', exit_date: '卖出日',
    entry_price: '买入价', score: '评分', tech_score: '技术面', fundamental_score: '基本面',
    档位_阈值: '档位', 档位_分位: '分位', pnl_pct: '收益', holding_days: '持有天数',
    信号: '触发信号', 方向: '方向', hit: '命中', 盘中分: '盘中分',
  };
  const cols = Object.keys(rows[0]);
  const table = U.el('table', 'val-table bt-table');
  const thead = U.el('thead');
  const trh = U.el('tr');
  cols.forEach(function (c) { trh.appendChild(U.el('th', '', labels[c] || c)); });
  thead.appendChild(trh);
  table.appendChild(thead);
  const tbody = U.el('tbody');
  rows.forEach(function (r) {
    const tr = U.el('tr', 'val-row');
    cols.forEach(function (c) {
      let v = r[c];
      let cls = '';
      if (c === 'pnl_pct') {
        const n = parseFloat(v);
        cls = isNaN(n) ? '' : (n > 0 ? 'up' : (n < 0 ? 'down' : 'flat'));
        v = isNaN(n) ? '—' : (n > 0 ? '+' : '') + n.toFixed(3) + '%';
      }
      tr.appendChild(U.el('td', cls, v == null || v === '' ? '—' : String(v)));
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  const wrap = U.el('div', 'bt-table-wrap');
  wrap.appendChild(U.el('div', 'bt-table-title',
    '事件明细（前 ' + rows.length + ' 条，完整 CSV 随运行产物保存）'));
  wrap.appendChild(table);
  return wrap;
}

function renderResult(meta) {
  const box = viewEl.querySelector('.bt-result');
  if (!box) return;
  box.innerHTML = '';
  const summary = meta.summary || {};
  const tables = meta.tables || {};

  const head = U.el('div', 'bt-result-head');
  head.appendChild(U.el('div', 'bt-result-title',
    (meta.strategy_name || '回测') + ' · ' + (meta.run_id || '')));
  if (meta.duration_ms) {
    head.appendChild(U.el('span', 'bt-dur', '耗时 ' + (meta.duration_ms / 1000).toFixed(1) + 's'));
  }
  const openBtn = U.el('button', 'btn btn-sm btn-primary', '↗ 打开完整看板');
  openBtn.onclick = function () { window.open('/api/backtest/run/' + meta.run_id + '/report', '_blank'); };
  head.appendChild(openBtn);
  box.appendChild(head);

  if (summary.confidence_note) {
    box.appendChild(U.el('div', 'bt-conf-note', summary.confidence_note));
  }
  box.appendChild(kpiCards(summary));

  TABLE_ORDER.forEach(function (key) {
    if (!(key in tables)) return;
    const el = renderTable(TABLE_TITLES[key] || key, tables[key]);
    if (el) box.appendChild(el);
  });
  Object.keys(tables).forEach(function (key) {
    if (TABLE_ORDER.indexOf(key) >= 0) return;
    const el = renderTable(key, tables[key]);
    if (el) box.appendChild(el);
  });

  loadTrades(meta.run_id, box);
}

async function loadTrades(runId, box) {
  try {
    const data = await API.backtestTrades(runId, 200);
    const el = renderTrades(data.rows || []);
    if (el) box.appendChild(el);
  } catch (e) { /* 明细缺失不阻塞结果页 */ }
}

// ------------------------------------------------------------------ 历史运行

async function loadHistory() {
  try {
    const data = await API.backtestRuns(10);
    state.runs = data.runs || [];
    renderHistory();
  } catch (e) { /* 静默 */ }
}

function renderHistory() {
  const box = viewEl.querySelector('.bt-history');
  if (!box) return;
  box.innerHTML = '';
  if (!state.runs.length) {
    box.appendChild(U.el('div', 'bt-empty', '暂无历史运行'));
    return;
  }
  state.runs.forEach(function (r) {
    const row = U.el('div', 'bt-hist-row');
    const statusCls = r.status === 'done' ? 'ok' : (r.status === 'failed' ? 'down' : 'warn');
    const statusText = { done: '完成', failed: '失败', running: '运行中', queued: '排队' }[r.status] || r.status;
    row.appendChild(U.el('span', 'bt-hist-status ' + statusCls, statusText));
    row.appendChild(U.el('span', 'bt-hist-name', r.strategy_name || r.strategy_id || ''));
    row.appendChild(U.el('span', 'bt-hist-time', r.created_at || ''));
    if (r.status === 'done' && r.win_rate_pct != null) {
      row.appendChild(U.el('span', 'bt-hist-stat',
        '样本 ' + (r.total_events || '—') + ' · 胜率 ' + r.win_rate_pct + '%' +
        (r.avg_return_pct != null ? ' · 均值 ' + (r.avg_return_pct > 0 ? '+' : '') + r.avg_return_pct + '%' : '')));
    } else if (r.status === 'failed') {
      row.appendChild(U.el('span', 'bt-hist-stat down', '已中断'));
    }
    const spacer = U.el('span', 'bt-hist-spacer');
    row.appendChild(spacer);
    if (r.status === 'done') {
      const viewBtn = U.el('button', 'btn btn-sm', '查看');
      viewBtn.onclick = async function () {
        try {
          const meta = await API.backtestRunStatus(r.run_id);
          state.runMeta = meta;
          renderResult(meta);
          // 滚到结果区
          const panel = viewEl.querySelector('.bt-result-panel');
          if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } catch (e) { U.toast('加载失败：' + (e.message || e), 'warn'); }
      };
      row.appendChild(viewBtn);
    }
    const delBtn = U.el('button', 'btn btn-sm bt-del', '删除');
    delBtn.onclick = async function () {
      try {
        await API.backtestDelete(r.run_id);
        loadHistory();
        U.toast('已删除', 'ok');
      } catch (e) { U.toast('删除失败：' + (e.message || e), 'warn'); }
    };
    row.appendChild(delBtn);
    box.appendChild(row);
  });
}

// ------------------------------------------------------------------ 入口

async function load() {
  if (!viewEl) return;
  skeleton();
  state.loading = true;
  try {
    const data = await API.backtestStrategies();
    state.strategies = data.strategies || [];
    if (data.running) {
      state.running = data.running;
      startPolling();
    }
    if (!state.current && state.strategies.length) {
      state.current = state.strategies[0];
    }
  } catch (e) {
    state.strategies = [];
    U.toast('策略清单加载失败：' + (e.message || e), 'warn');
  } finally {
    state.loading = false;
  }
  renderStrategies();
  renderForm();
  // 恢复进行中的进度条
  if (state.running) startPolling();
  loadHistory();

  const runBtn = viewEl.querySelector('.bt-run');
  if (runBtn) runBtn.onclick = submitRun;
}

export const PageBacktest = {
  mount: function () {
    viewEl = document.getElementById('view');
    load();
  },
  refresh: function () { load(); },
  tick: function () { /* 回测是手动触发型，不做自动刷新 */ },
};
