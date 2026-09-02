/* page-home（从 IIFE+global 转 ESM） */
import { U } from './util.js';
import { API } from './api.js';
import { AI } from './ai.js';
import { News } from './news.js';
import { App } from './app.js';


  const state = {
    items: [],
    sortKey: null,
    sortAsc: true,
    manage: false,
    selected: new Set(),
    lastPrices: {},
    dragCode: null,
    // 轮询在途标记：盘中 5s 心跳可能快于响应（数据源被频控时尤甚），
    // 无保护会堆叠并发请求，且先发后到的旧响应会把新价格覆盖回去
    ticking: false,
    // 首页 AI 摘要：code -> summary
    ai: {
      items: {},
      loading: false,
      error: null,
      total: 0,
      analyzed: 0
    },
    aiExpanded: null   // 当前行内展开 AI 面板的股票 code
  };

  function view() {
    return document.getElementById('view');
  }

  // AI 信号优先级：需要操作的股票排在前面（加仓 > 减仓 > 清仓 > 观望）
  const AI_ACTION_RANK = {
    '积极持仓/加仓': 3,
    '减仓规避': 2,
    '清仓离场': 1,
    '持有观望': 0
  };

  function aiSummary(item) {
    return state.ai.items[item.code] || null;
  }

  function aiSortKey(item) {
    const s = aiSummary(item);
    if (!s || !s.action) return -1;
    const rank = AI_ACTION_RANK[s.action] || 0;
    return rank * 1000 + (U.isNum(s.confidence) ? s.confidence : 0);
  }

  function sortedItems() {
    if (!state.sortKey) return state.items;
    const key = state.sortKey;
    const dir = state.sortAsc ? 1 : -1;
    return state.items.slice().sort(function (a, b) {
      let x, y;
      if (key === 'ai') {
        x = aiSortKey(a);
        y = aiSortKey(b);
      } else {
        x = a[key];
        y = b[key];
        if (key === 'name' || key === 'code') {
          return String(x || '').localeCompare(String(y || ''), 'zh-CN') * dir;
        }
        x = U.isNum(x) ? x : -Infinity;
        y = U.isNum(y) ? y : -Infinity;
      }
      return (x - y) * dir;
    });
  }

  function render() {
    const root = view();
    root.innerHTML = '';

    // 顶部快捷入口卡片（始终显示，让用户进首页一眼能看到回测入口）
    root.appendChild(renderQuickEntries());

    if (!state.items.length) {
      root.appendChild(renderEmpty());
      return;
    }

    // AI 总览卡片（放在表格上方，一眼看清持仓操作建议分布）
    root.appendChild(renderAIDashboard());

    const card = U.el('div', 'card');
    card.appendChild(renderToolbar());

    // 表头与数据行放进同一个横向滚动容器，窄屏下滚动时表头始终对齐
    const body = U.el('div', 'wl-table');
    body.id = 'wl-body';
    body.appendChild(renderHeadRow());
    sortedItems().forEach(function (item) {
      body.appendChild(renderRow(item));
      if (state.aiExpanded === item.code) {
        body.appendChild(renderAIInlinePanel(item));
      }
    });
    card.appendChild(body);
    root.appendChild(card);

    const tip = U.el('div', 'search-hint');
    tip.style.marginTop = '10px';
    tip.textContent = state.sortKey
      ? '当前为临时排序视图，拖拽排序请先点击「默认顺序」还原。'
      : '拖动左侧 ⠿ 手柄可调整自选股顺序，顺序会自动保存。';
    root.appendChild(tip);
  }

  // ---------------------------------------------------------- 首页快捷入口
  function renderQuickEntries() {
    const card = U.el('div', 'card entry-card');
    const grid = U.el('div', 'entry-grid');

    const entry = U.el('div', 'entry entry-backtest');
    entry.tabIndex = 0;
    entry.setAttribute('role', 'link');
    entry.setAttribute('aria-label', '跳转策略回测');

    const icon = U.el('div', 'entry-icon');
    icon.appendChild(U.icon('chartBar', { size: 28, cls: 'svg-icon' }));
    const body = U.el('div', 'entry-body');
    body.appendChild(U.el('div', 'entry-title', '策略回测'));
    body.appendChild(U.el('div', 'entry-desc',
      '用真实历史行情检验你的作业信号是否真的有区分度 · 三个策略事件研究'));
    const cta = U.el('div', 'entry-cta', '进入 →');

    entry.appendChild(icon);
    entry.appendChild(body);
    entry.appendChild(cta);

    const goBacktest = function () { location.hash = '#/backtest'; };
    entry.onclick = goBacktest;
    entry.onkeydown = function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); goBacktest(); }
    };

    grid.appendChild(entry);
    card.appendChild(grid);
    return card;
  }

  function renderEmpty() {
    const wrap = U.el('div', 'card');
    const box = U.el('div', 'empty');
    const emptyIcon = U.el('div', 'empty-icon');
    emptyIcon.appendChild(U.icon('chartLine', { size: 40, cls: 'svg-icon' }));
    box.appendChild(emptyIcon);
    box.appendChild(U.el('div', 'empty-title', '还没有自选股'));
    box.appendChild(U.el('div', 'empty-desc', '前往查询页搜索股票名称、代码或拼音首字母，添加到自选。'));
    const btn = U.el('button', 'btn btn-primary', '前往查询添加股票');
    btn.onclick = function () { location.hash = '#/search'; };
    box.appendChild(btn);
    wrap.appendChild(box);
    return wrap;
  }

  // ---------------------------------------------------------- AI 总览卡片
  function renderAIDashboard() {
    const card = U.el('div', 'card ai-dashboard');
    const head = U.el('div', 'ai-dashboard-head');
    const aiTitle = U.el('div', 'ai-dashboard-title');
    aiTitle.appendChild(U.icon('robot', { size: 16 }));
    aiTitle.appendChild(document.createTextNode(' AI 持仓总览'));
    head.appendChild(aiTitle);

    const refresh = U.el('button', 'btn btn-sm ai-batch-btn' + (state.ai.loading ? ' loading' : ''), state.ai.loading ? '分析中…' : '批量刷新 AI');
    refresh.disabled = state.ai.loading;
    refresh.onclick = function () { batchAIRefresh(refresh); };
    head.appendChild(refresh);
    card.appendChild(head);

    if (state.ai.error) {
      card.appendChild(U.el('div', 'notice', 'AI 摘要加载失败：' + state.ai.error));
      return card;
    }

    const summaries = Object.values(state.ai.items);
    const counts = { add: 0, hold: 0, reduce: 0, sell: 0, unknown: 0 };
    summaries.forEach(function (s) {
      if (s.action === '积极持仓/加仓') counts.add++;
      else if (s.action === '持有观望') counts.hold++;
      else if (s.action === '减仓规避') counts.reduce++;
      else if (s.action === '清仓离场') counts.sell++;
      else counts.unknown++;
    });

    const statRow = U.el('div', 'ai-dashboard-stats');
    function stat(cls, n, label) {
      const node = U.el('div', 'ai-stat ' + cls);
      node.appendChild(U.el('b', '', String(n)));
      node.appendChild(document.createTextNode(label));
      return node;
    }
    statRow.appendChild(stat('add', counts.add, '加仓'));
    statRow.appendChild(stat('hold', counts.hold, '观望'));
    statRow.appendChild(stat('reduce', counts.reduce, '减仓'));
    statRow.appendChild(stat('sell', counts.sell, '清仓'));
    statRow.appendChild(stat('unknown', state.items.length - summaries.length, '未分析'));
    card.appendChild(statRow);

    // Top3 需关注：按优先级 + 置信度排序
    const top = summaries.slice().sort(function (a, b) {
      const ra = AI_ACTION_RANK[a.action] || 0;
      const rb = AI_ACTION_RANK[b.action] || 0;
      if (ra !== rb) return rb - ra;
      return (b.confidence || 0) - (a.confidence || 0);
    }).slice(0, 3);

    if (top.length) {
      const topWrap = U.el('div', 'ai-dashboard-top');
      topWrap.appendChild(U.el('div', 'ai-dashboard-sub', 'Top 关注'));
      top.forEach(function (s) {
        const row = U.el('div', 'ai-top-row');
        const name = U.el('span', 'ai-top-name', s.name || s.code);
        name.onclick = function () { location.hash = '#/stock/' + s.code; };
        row.appendChild(name);
        row.appendChild(U.el('span', 'ai-top-code', s.code));
        const pill = renderAIPill(s, true);
        row.appendChild(pill);
        if (s.reason) {
          const reason = U.el('span', 'ai-top-reason', s.reason);
          reason.title = s.reason;
          row.appendChild(reason);
        }
        topWrap.appendChild(row);
      });
      card.appendChild(topWrap);
    }

    if (!summaries.length && !state.ai.loading) {
      card.appendChild(U.el('div', 'ai-dashboard-empty', '暂无 AI 摘要，点击「批量刷新 AI」生成全自选股的持仓建议。'));
    }

    return card;
  }

  function renderToolbar() {
    const bar = U.el('div', 'wl-toolbar');

    if (state.manage) {
      const all = U.el('input');
      all.type = 'checkbox';
      all.className = 'checkbox';
      all.checked = state.selected.size === state.items.length && state.items.length > 0;
      all.onchange = function () {
        state.selected = all.checked
          ? new Set(state.items.map(function (i) { return i.code; }))
          : new Set();
        render();
      };
      bar.appendChild(all);
      bar.appendChild(U.el('span', 'wl-count', '已选 ' + state.selected.size + ' / ' + state.items.length));

      const del = U.el('button', 'btn btn-sm btn-danger', '批量删除');
      del.disabled = state.selected.size === 0;
      del.onclick = function () { batchDelete(del); };
      bar.appendChild(del);

      const done = U.el('button', 'btn btn-sm', '完成');
      done.onclick = function () {
        state.manage = false;
        state.selected = new Set();
        render();
      };
      bar.appendChild(done);
    } else {
      const heading = U.el('div', 'wl-heading');
      heading.appendChild(U.el('div', 'wl-heading-title', '我的自选'));
      heading.appendChild(U.el('div', 'wl-heading-sub', state.items.length + ' 只股票 · 关键监测一览'));
      bar.appendChild(heading);
    }

    const sorters = U.el('div', 'sorters');
    [
      { key: null, label: '默认顺序' },
      { key: 'ai', label: 'AI 信号' },
      { key: 'change_pct', label: '涨跌幅' },
      { key: 'price', label: '现价' },
      { key: 'name', label: '名称' },
      { key: 'code', label: '代码' }
    ].forEach(function (opt) {
      const btn = U.el('button', 'sorter' + (state.sortKey === opt.key ? ' active' : ''));
      let label = opt.label;
      if (state.sortKey === opt.key && opt.key) label += state.sortAsc ? ' ↑' : ' ↓';
      btn.textContent = label;
      btn.onclick = function () {
        if (opt.key === null) {
          state.sortKey = null;
        } else if (state.sortKey === opt.key) {
          state.sortAsc = !state.sortAsc;
        } else {
          state.sortKey = opt.key;
          state.sortAsc = opt.key === 'name' || opt.key === 'code';
          // AI 信号默认倒序：需要操作的股票排在前面
          if (opt.key === 'ai') state.sortAsc = false;
        }
        render();
      };
      sorters.appendChild(btn);
    });
    bar.appendChild(sorters);
    return bar;
  }

  function renderHeadRow() {
    const row = U.el('div', 'wl-row wl-head');
    row.appendChild(U.el('div', '', state.manage ? '' : ''));
    row.appendChild(U.el('div', '', '股票'));
    row.appendChild(U.el('div', 'board-cell', '所属板块'));
    row.appendChild(withAlign(U.el('div', 'prev-cell', '昨收')));
    row.appendChild(withAlign(U.el('div', '', '现价')));
    row.appendChild(withAlign(U.el('div', 'vr-cell', '量比')));
    row.appendChild(withAlign(U.el('div', 'turnover-cell', '换手')));
    row.appendChild(withAlign(U.el('div', '', '涨跌幅')));
    row.appendChild(U.el('div', 'monitor-cell', '关键监测'));
    row.appendChild(U.el('div', 'ai-cell', 'AI 信号'));
    row.appendChild(withAlign(U.el('div', '', '操作')));
    return row;
  }

  function withAlign(node) {
    node.style.textAlign = 'right';
    return node;
  }

  function renderMonitorCell(item) {
    const monitor = item.monitor || {};
    const cell = U.el('div', 'monitor-cell');
    const tag = U.el('span', 'monitor-tag ' + (monitor.tone || 'flat'), monitor.action || '继续观察');
    tag.title = monitor.reason || '暂无监测说明';
    cell.appendChild(tag);
    return cell;
  }

  // AI 信号丸：action + confidence%，点击展开/收起行内 mini 面板
  const AI_PILL_CLASS = {
    '积极持仓/加仓': 'add',
    '持有观望': 'hold',
    '减仓规避': 'reduce',
    '清仓离场': 'sell'
  };

  function renderAIPill(summary, small) {
    const cls = AI_PILL_CLASS[summary.action] || 'hold';
    const pill = U.el('button', 'ai-pill ai-pill-' + cls + (small ? ' ai-pill-sm' : ''));
    const actionText = summary.action ? summary.action.replace('/加仓', '').replace('规避', '').replace('离场', '') : '未分析';
    pill.appendChild(document.createTextNode(actionText));
    if (U.isNum(summary.confidence)) {
      pill.appendChild(U.el('span', 'ai-pill-conf', summary.confidence + '%'));
    }
    pill.title = summary.reason || '点击展开 AI 摘要';
    return pill;
  }

  function renderAIInlinePanel(item) {
    const s = aiSummary(item);
    const panel = U.el('div', 'wl-row ai-inline-panel');
    if (!s) {
      panel.appendChild(U.el('div', 'ai-inline-empty', '暂无 AI 摘要'));
      return panel;
    }

    const wrap = U.el('div', 'ai-inline-wrap');
    const head = U.el('div', 'ai-inline-head');
    head.appendChild(U.el('span', 'ai-inline-action ai-pill ai-pill-' + (AI_PILL_CLASS[s.action] || 'hold'),
      s.action || '持有观望'));
    if (s.reason) head.appendChild(U.el('span', 'ai-inline-reason', s.reason));
    wrap.appendChild(head);

    const grids = U.el('div', 'ai-inline-grids');
    function kv(label, val) {
      const node = U.el('div', 'ai-inline-kv');
      node.appendChild(U.el('span', 'ai-inline-k', label));
      node.appendChild(U.el('span', 'ai-inline-v', val || '--'));
      return node;
    }
    grids.appendChild(kv('支撑', U.price(s.support)));
    grids.appendChild(kv('压力', U.price(s.resistance)));
    grids.appendChild(kv('介入', s.entry_zone));
    grids.appendChild(kv('离场', s.exit_zone));
    grids.appendChild(kv('止损', U.price(s.stop_loss)));
    grids.appendChild(kv('止盈', U.price(s.take_profit)));
    grids.appendChild(kv('周期', s.horizon));
    grids.appendChild(kv('引擎', s.engine === 'llm' ? 'AI 大模型' : '规则引擎'));
    wrap.appendChild(grids);

    if (s.confidence_reason) {
      wrap.appendChild(U.el('div', 'ai-inline-conf', '置信度依据：' + s.confidence_reason));
    }

    const foot = U.el('div', 'ai-inline-foot');
    const fullBtn = U.el('button', 'btn btn-sm btn-primary', '查看完整 AI 分析');
    fullBtn.onclick = function (e) {
      e.stopPropagation();
      AI.open(item.code, item.name, fullBtn);
    };
    foot.appendChild(fullBtn);
    const closeBtn = U.el('button', 'btn btn-sm', '收起');
    closeBtn.onclick = function (e) {
      e.stopPropagation();
      state.aiExpanded = null;
      render();
    };
    foot.appendChild(closeBtn);
    wrap.appendChild(foot);

    panel.appendChild(wrap);
    return panel;
  }

  function renderRow(item) {
    const row = U.el('div', 'wl-row');
    row.dataset.code = item.code;

    // 拖拽手柄 / 多选框
    const first = U.el('div', 'drag-handle');
    if (state.manage) {
      const cb = U.el('input');
      cb.type = 'checkbox';
      cb.className = 'checkbox';
      cb.checked = state.selected.has(item.code);
      cb.onchange = function () {
        if (cb.checked) state.selected.add(item.code); else state.selected.delete(item.code);
        render();
      };
      first.className = '';
      first.style.textAlign = 'center';
      first.appendChild(cb);
    } else {
      first.textContent = '⠿';
      first.title = '拖动排序';
      if (!state.sortKey) {
        row.draggable = true;
        bindDrag(row, item.code);
      } else {
        first.style.opacity = '.3';
        first.title = '临时排序视图下不可拖拽';
      }
    }
    row.appendChild(first);

    // 名称 + 代码
    const cell = U.el('div', 'stock-cell');
    const nameLine = U.el('div', 'stock-name', item.name || item.code);
    if (item.status && item.status !== 'normal') {
      const badge = U.el('span', 'tag warn', item.status === 'suspended' ? '停牌' : '延迟');
      badge.style.marginLeft = '6px';
      badge.style.fontWeight = '400';
      nameLine.appendChild(badge);
    }
    cell.appendChild(nameLine);
    cell.appendChild(U.el('div', 'stock-code', item.code));
    row.appendChild(cell);

    // 板块
    const boardCell = U.el('div', 'board-cell');
    const tags = U.el('div', 'board-tags');
    if (item.board) tags.appendChild(U.el('span', 'tag', item.board));
    else tags.appendChild(U.el('span', 'tag', '--'));
    boardCell.appendChild(tags);
    row.appendChild(boardCell);

    // 昨收
    row.appendChild(U.el('div', 'prev-cell', U.price(item.prev_close)));

    // 现价（带涨跌色 + 变动闪烁）
    const priceCell = U.el('div', 'price-cell ' + U.tone(item.change_pct), U.price(item.price));
    row.appendChild(priceCell);

    // 量比
    row.appendChild(U.el('div', 'vr-cell', U.ratio(item.volume_ratio)));

    // 换手率
    row.appendChild(U.el('div', 'turnover-cell', U.turnover(item.turnover)));

    // 涨跌幅
    row.appendChild(U.el('div', 'pct-cell ' + U.tone(item.change_pct), U.pct(item.change_pct)));

    // 关键监测
    row.appendChild(renderMonitorCell(item));

    // AI 信号丸
    const aiCell = U.el('div', 'ai-cell');
    const summary = aiSummary(item);
    if (summary) {
      const pill = renderAIPill(summary, false);
      pill.onclick = function (e) {
        e.stopPropagation();
        state.aiExpanded = state.aiExpanded === item.code ? null : item.code;
        render();
      };
      aiCell.appendChild(pill);
    } else {
      aiCell.appendChild(U.el('span', 'ai-pill ai-pill-none', '未分析'));
    }
    row.appendChild(aiCell);

    // 操作
    const actions = U.el('div', 'row-actions');
    const detailBtn = U.el('button', 'btn btn-sm', '详情');
    detailBtn.onclick = function (e) {
      e.stopPropagation();
      location.hash = '#/stock/' + item.code;
    };
    const aiBtn = U.el('button', 'btn btn-sm btn-primary', 'AI分析');
    aiBtn.onclick = function (e) {
      e.stopPropagation();
      AI.open(item.code, item.name, aiBtn);
    };
    const newsBtn = U.el('button', 'btn btn-sm', '资讯');
    newsBtn.onclick = function (e) {
      e.stopPropagation();
      News.open(item.code, item.name, newsBtn);
    };
    const delBtn = U.el('button', 'btn btn-sm btn-danger', '删除');
    delBtn.title = '从自选股中删除';
    delBtn.onclick = function (e) {
      e.stopPropagation();
      removeOne(item, delBtn);
    };
    actions.appendChild(detailBtn);
    actions.appendChild(aiBtn);
    actions.appendChild(newsBtn);
    actions.appendChild(delBtn);
    row.appendChild(actions);

    // 价格变动闪烁
    const prev = state.lastPrices[item.code];
    if (U.isNum(prev) && U.isNum(item.price) && prev !== item.price) {
      row.classList.add(item.price > prev ? 'flash-up' : 'flash-down');
      setTimeout(function () { row.classList.remove('flash-up', 'flash-down'); }, 620);
    }
    state.lastPrices[item.code] = item.price;

    return row;
  }

  // ---------------------------------------------------------- 拖拽排序
  function bindDrag(row, code) {
    row.addEventListener('dragstart', function (e) {
      state.dragCode = code;
      row.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', code); } catch (err) { /* IE */ }
    });
    row.addEventListener('dragend', function () {
      row.classList.remove('dragging');
      document.querySelectorAll('.wl-row.drag-over').forEach(function (n) {
        n.classList.remove('drag-over');
      });
      state.dragCode = null;
    });
    row.addEventListener('dragover', function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      if (state.dragCode && state.dragCode !== code) row.classList.add('drag-over');
    });
    row.addEventListener('dragleave', function () {
      row.classList.remove('drag-over');
    });
    row.addEventListener('drop', function (e) {
      e.preventDefault();
      row.classList.remove('drag-over');
      const from = state.dragCode;
      if (!from || from === code) return;
      const list = state.items.slice();
      const fromIdx = list.findIndex(function (i) { return i.code === from; });
      const toIdx = list.findIndex(function (i) { return i.code === code; });
      if (fromIdx < 0 || toIdx < 0) return;
      const [moved] = list.splice(fromIdx, 1);
      list.splice(toIdx, 0, moved);
      state.items = list;
      render();
      API.reorder(list.map(function (i) { return i.code; })).catch(function (err) {
        U.toast('排序保存失败：' + err.message, 'err');
      });
    });
  }

  // ---------------------------------------------------------- 删除
  /** 单只删除：轮询 tick 会整表重载，请求在途期间先禁用按钮避免重复提交 */
  async function removeOne(item, btn) {
    if (btn.disabled) return;
    const label = (item.name || '') + '（' + item.code + '）';
    if (!await U.confirmAt(btn, '从自选股中删除 ' + label + '？', { okText: '删除' })) return;
    btn.disabled = true;
    try {
      await API.removeWatch([item.code]);
      state.selected.delete(item.code);
      U.toast('已删除 ' + label, 'ok');
      await load(true);
    } catch (err) {
      btn.disabled = false;
      U.toast('删除失败：' + err.message, 'err');
    }
  }

  async function batchDelete(btn) {
    const codes = Array.from(state.selected);
    if (!codes.length) return;
    if (!await U.confirmAt(btn, '从自选股中删除选中的 ' + codes.length + ' 只股票？',
      { okText: '删除' })) return;
    try {
      await API.removeWatch(codes);
      state.selected = new Set();
      U.toast('已删除 ' + codes.length + ' 只自选股', 'ok');
      await load(true);
    } catch (err) {
      U.toast('删除失败：' + err.message, 'err');
    }
  }

  // ---------------------------------------------------------- 数据加载
  async function load(force) {
    try {
      const data = await API.watchlist(force);
      state.items = data.items || [];
      // 清理已删除股票的选中态
      const codes = new Set(state.items.map(function (i) { return i.code; }));
      state.selected.forEach(function (c) { if (!codes.has(c)) state.selected.delete(c); });
      render();
      App.setSession(data.session);
    } catch (err) {
      const root = view();
      if (!state.items.length) {
        root.innerHTML = '<div class="card"><div class="empty">'
          + '<div class="empty-icon">' + U.iconHtml('alert', { size: 40 }) + '</div>'
          + '<div class="empty-title">数据加载失败</div>'
          + '<div class="empty-desc">' + U.escapeHtml(err.message) + '</div>'
          + '</div></div>';
      } else {
        U.toast('刷新失败：' + err.message, 'err');
      }
    }
  }

  // ---------------------------------------------------------- AI 摘要加载
  async function loadAI(refresh) {
    if (state.ai.loading) return;
    state.ai.loading = true;
    state.ai.error = null;
    if (!refresh) render();
    try {
      const data = await API.aiWatchlist(refresh);
      const map = {};
      (data.items || []).forEach(function (s) { if (s.code) map[s.code] = s; });
      state.ai.items = map;
      state.ai.total = data.total || 0;
      state.ai.analyzed = data.analyzed || 0;
    } catch (err) {
      state.ai.error = err.message || String(err);
      U.toast('AI 摘要加载失败：' + state.ai.error, 'err');
    } finally {
      state.ai.loading = false;
      render();
    }
  }

  async function batchAIRefresh(btn) {
    if (state.ai.loading) return;
    btn = btn || {};
    U.toast('开始批量分析 ' + state.items.length + ' 只自选股…', 'ok');
    try {
      await loadAI(true);
      U.toast('AI 摘要已更新：' + state.ai.analyzed + ' / ' + state.ai.total, 'ok');
    } catch (err) {
      U.toast('批量 AI 分析失败：' + (err.message || String(err)), 'err');
    }
  }

  /** 静默刷新：只更新价格，不重建 DOM，避免打断拖拽和滚动 */
  async function tick() {
    if (state.manage) return;
    if (state.ticking) return;            // 上一轮未回，跳过本轮
    state.ticking = true;
    try {
      const data = await API.watchlist(false);
      const items = data.items || [];
      const map = {};
      items.forEach(function (i) { map[i.code] = i; });
      // 自选列表可能在别处变化（如快讯弹窗/查询页加入、多标签页同时打开）：
      // 新增/删除会改变列表结构，静默补价补不了新行，直接整表重载自动同步
      const known = new Set(state.items.map(function (i) { return i.code; }));
      if (items.length !== state.items.length || items.some(function (i) { return !known.has(i.code); })) {
        state.items = items;
        render();
        App.setSession(data.session);
        return;
      }
      if (!state.items.length) return;
      state.items = state.items.map(function (old) {
        return map[old.code] || old;
      });
      patchPrices();
      App.setSession(data.session);
    } catch (err) { /* 静默失败，下一轮再试 */ } finally {
      state.ticking = false;
    }
  }

  function patchPrices() {
    state.items.forEach(function (item) {
      const row = document.querySelector('.wl-row[data-code="' + item.code + '"]');
      if (!row) return;
      const priceCell = row.querySelector('.price-cell');
      const pctCell = row.querySelector('.pct-cell');
      const prevCell = row.querySelector('.prev-cell');
      const vrCell = row.querySelector('.vr-cell');
      const turnoverCell = row.querySelector('.turnover-cell');
      const monitorCell = row.querySelector('.monitor-cell');
      if (!priceCell) return;

      const prev = state.lastPrices[item.code];
      if (U.isNum(prev) && U.isNum(item.price) && prev !== item.price) {
        row.classList.remove('flash-up', 'flash-down');
        void row.offsetWidth; // 重启动画
        row.classList.add(item.price > prev ? 'flash-up' : 'flash-down');
      }
      state.lastPrices[item.code] = item.price;

      const tone = U.tone(item.change_pct);
      priceCell.textContent = U.price(item.price);
      priceCell.className = 'price-cell ' + tone;
      if (pctCell) {
        pctCell.textContent = U.pct(item.change_pct);
        pctCell.className = 'pct-cell ' + tone;
      }
      if (prevCell) prevCell.textContent = U.price(item.prev_close);
      if (vrCell) vrCell.textContent = U.ratio(item.volume_ratio);
      if (turnoverCell) turnoverCell.textContent = U.turnover(item.turnover);
      if (monitorCell) {
        const next = renderMonitorCell(item);
        monitorCell.replaceWith(next);
      }
    });
  }

  export const PageHome = {
    mount: function () {
      state.sortKey = null;
      state.manage = false;
      state.selected = new Set();
      state.aiExpanded = null;
      view().innerHTML = '<div class="card"><div class="loading-block">加载自选股…</div></div>';
      return load(false).then(function () {
        // 自选加载完成后再加载 AI 摘要（不阻塞首屏）
        return loadAI(false);
      });
    },
    refresh: function () {
      return load(true).then(function () { return loadAI(false); });
    },
    tick: tick,
    toggleManage: function () {
      if (!state.items.length) {
        U.toast('还没有自选股', 'err');
        return;
      }
      state.manage = !state.manage;
      state.selected = new Set();
      render();
    }
  };
