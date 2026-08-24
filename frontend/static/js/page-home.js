/* 首页：自选股看板（需求 3.x） */
(function (global) {
  'use strict';

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
    ticking: false
  };

  function view() {
    return document.getElementById('view');
  }

  function sortedItems() {
    if (!state.sortKey) return state.items;
    const key = state.sortKey;
    const dir = state.sortAsc ? 1 : -1;
    return state.items.slice().sort(function (a, b) {
      let x = a[key], y = b[key];
      if (key === 'name' || key === 'code') {
        return String(x || '').localeCompare(String(y || ''), 'zh-CN') * dir;
      }
      x = U.isNum(x) ? x : -Infinity;
      y = U.isNum(y) ? y : -Infinity;
      return (x - y) * dir;
    });
  }

  function render() {
    const root = view();
    root.innerHTML = '';

    if (!state.items.length) {
      root.appendChild(renderEmpty());
      return;
    }

    const card = U.el('div', 'card');
    card.appendChild(renderToolbar());

    // 表头与数据行放进同一个横向滚动容器，窄屏下滚动时表头始终对齐
    const body = U.el('div', 'wl-table');
    body.id = 'wl-body';
    body.appendChild(renderHeadRow());
    sortedItems().forEach(function (item) {
      body.appendChild(renderRow(item));
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

  function renderEmpty() {
    const wrap = U.el('div', 'card');
    const box = U.el('div', 'empty');
    box.appendChild(U.el('div', 'empty-icon', '📈'));
    box.appendChild(U.el('div', 'empty-title', '还没有自选股'));
    box.appendChild(U.el('div', 'empty-desc', '前往查询页搜索股票名称、代码或拼音首字母，添加到自选。'));
    const btn = U.el('button', 'btn btn-primary', '前往查询添加股票');
    btn.onclick = function () { location.hash = '#/search'; };
    box.appendChild(btn);
    wrap.appendChild(box);
    return wrap;
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
      del.onclick = batchDelete;
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
    if (!confirm('确定从自选股中删除 ' + label + '？')) return;
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

  async function batchDelete() {
    const codes = Array.from(state.selected);
    if (!codes.length) return;
    if (!confirm('确定从自选股中删除这 ' + codes.length + ' 只股票？')) return;
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
          + '<div class="empty-icon">⚠️</div>'
          + '<div class="empty-title">数据加载失败</div>'
          + '<div class="empty-desc">' + U.escapeHtml(err.message) + '</div>'
          + '</div></div>';
      } else {
        U.toast('刷新失败：' + err.message, 'err');
      }
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

  global.PageHome = {
    mount: function () {
      state.sortKey = null;
      state.manage = false;
      state.selected = new Set();
      view().innerHTML = '<div class="card"><div class="loading-block">加载自选股…</div></div>';
      return load(false);
    },
    refresh: function () { return load(true); },
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
})(window);
