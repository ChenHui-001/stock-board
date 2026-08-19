/* 热点追踪页：近 30 分钟市场热点（同花顺/东方财富/新浪财经 7x24 快讯聚合） */
(function (global) {
  'use strict';

  const state = {
    items: [],
    meta: null,
    filter: 'all',
    loading: false,
    error: null,
    lastAuto: 0
  };

  function view() { return document.getElementById('view'); }

  function isCurrent() {
    // 异步加载返回时用户可能已切走页面：仅在仍停留在热点页时才重渲染，
    // 避免 in-flight 的 load() 把其他页面内容覆盖掉
    return (location.hash || '').indexOf('hotspot') >= 0;
  }

  function originCounts() {
    const counts = {};
    state.items.forEach(function (it) {
      counts[it.origin] = (counts[it.origin] || 0) + 1;
    });
    return counts;
  }

  function filteredItems() {
    if (state.filter === 'all') return state.items;
    return state.items.filter(function (it) { return it.origin === state.filter; });
  }

  function render() {
    const root = view();
    root.innerHTML = '';
    const card = U.el('div', 'card');
    card.appendChild(renderHead());
    card.appendChild(renderFilters());
    const listHost = U.el('div', 'hotspot-list');
    listHost.id = 'hotspot-list';
    card.appendChild(listHost);
    renderListInto(listHost);
    root.appendChild(card);
  }

  function renderHead() {
    const head = U.el('div', 'card-head hotspot-head');
    const left = U.el('div');
    left.appendChild(U.el('div', 'card-title', '🔥 热点追踪'));
    const sub = U.el('div', 'card-sub');
    if (state.error) {
      sub.textContent = state.error;
    } else if (state.meta) {
      sub.textContent = '近 ' + state.meta.window_minutes + ' 分钟 · ' + state.meta.total + ' 条'
        + ' · 更新于 ' + ((state.meta.fetched_at || '').slice(11, 19) || '--');
    } else {
      sub.textContent = '正在聚合多源 7x24 快讯…';
    }
    left.appendChild(sub);
    head.appendChild(left);

    const refresh = U.el('button', 'btn btn-sm', '⟳ 刷新');
    refresh.onclick = function () { load(true); };
    head.appendChild(refresh);
    return head;
  }

  function renderFilters() {
    const bar = U.el('div', 'hotspot-filters');
    const counts = originCounts();
    const opts = [{ key: 'all', label: '全部 ' + state.items.length }].concat(
      Object.keys(counts).map(function (k) {
        return { key: k, label: k + ' ' + counts[k] };
      })
    );
    opts.forEach(function (opt) {
      const chip = U.el('button', 'chip' + (state.filter === opt.key ? ' active' : ''), opt.label);
      chip.onclick = function () {
        state.filter = opt.key;
        // 同步高亮当前筛选 chip，并只重建列表（不打断页面滚动）
        bar.querySelectorAll('.chip').forEach(function (c) {
          c.classList.toggle('active', c === chip);
        });
        const host = document.getElementById('hotspot-list');
        if (host) renderListInto(host);
      };
      bar.appendChild(chip);
    });
    return bar;
  }

  function renderListInto(host) {
    host.innerHTML = '';

    if (state.loading && !state.items.length) {
      host.appendChild(U.el('div', 'loading-block', '正在抓取热点快讯…'));
      return;
    }
    if (state.error && !state.items.length) {
      const empty = U.el('div', 'empty');
      empty.appendChild(U.el('div', 'empty-icon', '📡'));
      empty.appendChild(U.el('div', 'empty-title', '热点暂不可用'));
      empty.appendChild(U.el('div', 'empty-desc', state.error));
      host.appendChild(empty);
      return;
    }

    const items = filteredItems();
    if (!items.length) {
      const empty = U.el('div', 'empty');
      empty.appendChild(U.el('div', 'empty-icon', '🕐'));
      empty.appendChild(U.el('div', 'empty-title', '近 ' + (state.meta ? state.meta.window_minutes : 30) + ' 分钟暂无热点'));
      empty.appendChild(U.el('div', 'empty-desc', '当前来源暂无新快讯，稍后会自动刷新。'));
      host.appendChild(empty);
      return;
    }

    items.forEach(function (it) {
      host.appendChild(renderItem(it));
    });
  }

  function renderItem(it) {
    const row = U.el('div', 'hotspot-item');

    const time = U.el('div', 'hotspot-time', (it.time || '').slice(11, 16) || '--');
    row.appendChild(time);

    const body = U.el('div', 'hotspot-body');

    const metaLine = U.el('div', 'hotspot-meta');
    const src = U.el('span', 'tag' + (it.media_badge ? ' hotspot-hot' : ''), it.source || it.origin || '--');
    src.title = (it.origin || '') + ' · ' + (it.source || '');
    metaLine.appendChild(src);
    const origin = U.el('span', 'hotspot-origin', it.origin || '');
    metaLine.appendChild(origin);
    body.appendChild(metaLine);

    if (it.url) {
      const a = U.el('a', 'hotspot-title', it.title);
      a.href = it.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      body.appendChild(a);
    } else {
      body.appendChild(U.el('div', 'hotspot-title', it.title));
    }

    if (it.summary) {
      const sum = U.el('div', 'hotspot-summary', it.summary);
      sum.title = it.summary;
      body.appendChild(sum);
    }
    row.appendChild(body);
    return row;
  }

  async function load(force) {
    if (state.loading) return;
    state.loading = true;
    if (isCurrent() && !state.items.length) render();
    try {
      const data = await API.hotspot(30, force);
      state.items = data.items || [];
      state.meta = data.meta || null;
      state.error = (data.meta && data.meta.error) || null;
      // 当前筛选来源已无条目时回退到「全部」，避免空列表误导
      if (state.filter !== 'all' && !state.items.some(function (it) { return it.origin === state.filter; })) {
        state.filter = 'all';
      }
    } catch (err) {
      state.error = err.message || String(err);
      // 已有数据时保留旧列表，仅提示；无数据时才弹 toast
      if (!state.items.length) U.toast('热点加载失败：' + state.error, 'err');
    } finally {
      state.loading = false;
      // 成功与失败都计一次自动刷新节流：数据源故障期间不让 5s tick 反复重打后端
      state.lastAuto = Date.now();
      if (isCurrent()) render();
    }
  }

  global.PageHotspot = {
    mount: function () {
      state.filter = 'all';
      state.error = null;
      render();
      return load(false);
    },
    refresh: function () { return load(true); },
    tick: function () {
      // 自动刷新节流到 60s：后端聚合结果本身有 90s 缓存，无需每 5s 打一次
      if (Date.now() - state.lastAuto < 60000) return;
      load(false);
    }
  };
})(window);
