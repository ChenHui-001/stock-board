/* page-hotspot（从 IIFE+global 转 ESM） */
import { U } from './util.js';
import { API } from './api.js';
import { AI } from './ai.js';


  const state = {
    items: [],
    meta: null,
    filter: 'all',
    sector: null,       // 当前选中的概念过滤
    q: '',
    minutes: 30,        // 快讯时间窗（分钟），可切 15/30/60
    loading: false,
    error: null,
    lastAuto: 0,
    // 服务端关键词检索（真搜索，不是过滤当前页）
    search: {
      q: '',            // 已发起检索的关键词，用于丢弃过期响应
      days: 7,
      items: [],
      meta: null,
      loading: false,
      error: null
    },
    // 行内相关股面板
    inline: { item: null, loading: false, data: null, error: null }
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
    const q = (state.q || '').toLowerCase();
    return state.items.filter(function (it) {
      if (state.filter !== 'all' && it.origin !== state.filter) return false;
      if (state.sector) {
        const tags = it.tags || [];
        if (!tags.some(function (t) { return t.name === state.sector; })) return false;
      }
      if (!q) return true;
      return (it.title || '').toLowerCase().indexOf(q) >= 0
        || (it.summary || '').toLowerCase().indexOf(q) >= 0
        || (it.source || '').toLowerCase().indexOf(q) >= 0
        || (it.origin || '').toLowerCase().indexOf(q) >= 0;
    });
  }

  function render() {
    const root = view();
    root.innerHTML = '';
    const card = U.el('div', 'card');
    card.appendChild(renderHead());
    card.appendChild(renderSectorHeat());
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
      // 数据源健康度徽标：6/6 正常 / 部分失败 / 全部失败
      const badge = renderSourceHealthBadge();
      if (badge) sub.appendChild(badge);
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

  // 数据源健康度徽标：state.meta.sources 含每源 ok/count/error，渲染成单个小标签。
  // 三档配色：全部正常 .ok / 部分失败 .warn / 全部失败 .err；
  // 鼠标悬浮展示每源明细（title 属性），便于一眼定位故障源。
  function renderSourceHealthBadge() {
    const sources = (state.meta && state.meta.sources) || [];
    if (!sources.length) return null;
    const okCount = sources.filter(function (s) { return s.ok; }).length;
    const total = sources.length;
    const tier = okCount === total ? 'ok' : (okCount === 0 ? 'err' : 'warn');
    const failed = sources.filter(function (s) { return !s.ok; });
    let text = okCount + '/' + total + ' 源' + (tier === 'ok' ? '正常' : (tier === 'err' ? '不可用' : '故障'));
    // 故障时把失败源名也带上：「4/6 源故障（财联社/金十）」
    if (tier === 'warn' && failed.length) {
      text += '（' + failed.map(function (s) { return s.name; }).join(' / ') + '）';
    }
    const badge = U.el('span', 'tag hotspot-source-stat hotspot-source-' + tier, text);
    // title 展示每源明细：成功源含条数，失败源含错误原因
    badge.title = sources.map(function (s) {
      return (s.ok ? '✓ ' : '✗ ') + s.name + ': ' + (s.ok ? s.count + ' 条' : s.error || '失败');
    }).join('\n');
    return badge;
  }

  // 热点概念榜单：从 meta.sector_heat 渲染可点击的概念 chips，点击后过滤当前快讯流。
  function renderSectorHeat() {
    const heat = (state.meta && state.meta.sector_heat) || [];
    const wrap = U.el('div', 'sector-heat');

    // 头部：标题 + 选中时显示「清除」
    const head = U.el('div', 'sector-heat-head');
    const titleWrap = U.el('div', 'sector-heat-title-wrap');
    titleWrap.appendChild(U.el('span', 'sector-heat-icon', '🔥'));
    titleWrap.appendChild(U.el('span', 'sector-heat-title', '热点概念'));
    titleWrap.appendChild(U.el('span', 'sector-heat-sub', heat.length ? '近 ' + state.minutes + ' 分钟提及趋势' : ''));
    head.appendChild(titleWrap);
    if (state.sector) {
      const clear = U.el('button', 'sector-heat-clear', '清除');
      clear.onclick = function () {
        state.sector = null;
        render();
      };
      head.appendChild(clear);
    }
    wrap.appendChild(head);

    if (!heat.length) {
      wrap.appendChild(U.el('div', 'sector-heat-empty', '暂无概念标签'));
      return wrap;
    }

    const row = U.el('div', 'sector-heat-row');
    // 「全部」chip，方便一键清除
    const allChip = U.el('button', 'sector-chip' + (state.sector ? '' : ' active'));
    allChip.appendChild(U.el('span', 'sector-chip-name', '全部'));
    allChip.appendChild(U.el('span', 'sector-chip-count', String(state.items.length)));
    allChip.onclick = function () {
      state.sector = null;
      render();
    };
    row.appendChild(allChip);

    heat.slice(0, 12).forEach(function (s) {
      const cls = 'sector-chip' + (state.sector === s.name ? ' active' : '')
        + ' sector-trend-' + (s.trend || 'flat');
      const chip = U.el('button', cls);
      const trendIcon = { up: '▲', down: '▼', flat: '—' }[s.trend || 'flat'];
      const trendLabel = { up: '发酵', down: '退潮', flat: '持平' }[s.trend || 'flat'];
      chip.appendChild(U.el('span', 'sector-chip-trend', trendIcon));
      chip.appendChild(U.el('span', 'sector-chip-name', s.name));
      chip.appendChild(U.el('span', 'sector-chip-count', String(s.total)));
      chip.title = s.name + '：' + s.total + ' 条，趋势' + trendLabel
        + '（利好 ' + s.bull + ' / 利空 ' + s.bear + ' / 中性 ' + s.neutral + '）';
      chip.onclick = function () {
        state.sector = state.sector === s.name ? null : s.name;
        render();
      };
      row.appendChild(chip);
    });
    wrap.appendChild(row);
    return wrap;
  }

  function renderFilters() {
    const bar = U.el('div', 'hotspot-filters');

    // 来源筛选
    const srcWrap = U.el('div', 'hotspot-filter-group hotspot-source-group');
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
        repaintList();
      };
      srcWrap.appendChild(chip);
    });
    bar.appendChild(srcWrap);

    // 右侧：时间窗 + 搜索
    const right = U.el('div', 'hotspot-filter-group hotspot-filter-right');

    // 时间窗：后端支持 5~120 分钟，改动即按新窗口重取
    const range = U.el('div', 'hotspot-range');
    [15, 30, 60].forEach(function (m) {
      const b = U.el('button', 'range-btn' + (state.minutes === m ? ' active' : ''), m + ' 分钟');
      b.title = '只看最近 ' + m + ' 分钟的快讯';
      b.onclick = function () {
        if (state.minutes === m) return;
        state.minutes = m;
        load(false);
      };
      range.appendChild(b);
    });
    right.appendChild(range);

    // 关键词检索：打服务端（不是过滤当前页），只重建列表不打断页面滚动
    const search = U.el('input', 'hotspot-search');
    search.type = 'text';
    search.maxLength = 32;
    search.placeholder = '搜索资讯（全网检索，回车即搜）…';
    search.value = state.q;
    search.setAttribute('aria-label', '搜索资讯');
    search.oninput = function () {
      state.q = search.value;
      repaintList();          // 先用本地命中占位，0 延迟
      onSearchInput(search.value);
    };
    search.onkeydown = function (e) {
      if (e.key === 'Enter') { state.q = search.value; doSearch(search.value); }
      if (e.key === 'Escape' && search.value) { clearSearch(); }
    };
    right.appendChild(search);
    bar.appendChild(right);
    return bar;
  }

  function repaintList() {
    const host = document.getElementById('hotspot-list');
    if (host) renderListInto(host);
  }

  // ---------------------------------------------------------- 服务端关键词检索
  // 搜索框语义：原先只对已加载的 ≤40 条快讯做子串匹配（"只能搜当前页面"），
  // 现在防抖 280ms 后打 /api/hotspot/search，关键词直达上游检索库。
  // 本地命中先行渲染（0 延迟），服务端结果回来后替换。
  const onSearchInput = U.debounce(function (kw) { doSearch(kw); }, 280);

  async function doSearch(keyword) {
    const kw = (keyword || '').trim();
    if (!kw) {
      state.search.q = '';
      state.search.items = [];
      state.search.meta = null;
      state.search.error = null;
      state.search.loading = false;
      repaintList();
      return;
    }
    state.search.q = kw;
    state.search.loading = true;
    state.search.error = null;
    repaintList();
    try {
      const data = await API.hotspotSearch(kw, state.search.days);
      if ((state.q || '').trim() !== kw) return;   // 输入框已变，丢弃过期响应
      state.search.items = data.items || [];
      state.search.meta = data.meta || null;
      state.search.error = (data.meta && data.meta.error) || null;
    } catch (err) {
      if ((state.q || '').trim() !== kw) return;
      state.search.items = [];
      state.search.meta = null;
      state.search.error = err.message || String(err);
    } finally {
      if ((state.q || '').trim() === kw) {
        state.search.loading = false;
        repaintList();
      }
    }
  }

  function clearSearch() {
    state.q = '';
    state.search.q = '';
    state.search.items = [];
    state.search.meta = null;
    state.search.error = null;
    state.search.loading = false;
    const box = document.querySelector('.hotspot-search');
    if (box) box.value = '';
    repaintList();
  }

  // 检索结果头：条数 + 检索来源 + 回溯天数 + 清除
  function renderSearchHead(kw, serverReady, shownCount) {
    const bar = U.el('div', 'hotspot-search-head');
    const info = U.el('div', 'hotspot-search-info');
    if (state.search.loading && !serverReady) {
      info.appendChild(U.el('span', 'hotspot-search-count', '正在检索「' + kw + '」…'));
      const local = filteredItems().length;
      if (local) info.appendChild(U.el('span', 'hotspot-search-src', '先显示当前流内匹配 ' + local + ' 条'));
    } else if (state.search.error) {
      info.appendChild(U.el('span', 'hotspot-search-count', '检索失败'));
      info.appendChild(U.el('span', 'hotspot-search-src', state.search.error));
    } else {
      info.appendChild(U.el('span', 'hotspot-search-count', '共 ' + shownCount + ' 条'));
      const src = (state.search.meta && state.search.meta.engine_label) || '东方财富全文检索';
      info.appendChild(U.el('span', 'hotspot-search-src', '来源 ' + src));
      if (state.search.meta && state.search.meta.fallback_from) {
        info.appendChild(U.el('span', 'hotspot-search-src',
          '（全网搜索不可用，已回退站内检索）'));
      }
    }
    bar.appendChild(info);

    const acts = U.el('div', 'hotspot-search-acts');
    // 回溯天数：改动即重新检索
    [{ d: 1, t: '1 天' }, { d: 7, t: '7 天' }, { d: 30, t: '30 天' }].forEach(function (o) {
      const b = U.el('button', 'range-btn' + (state.search.days === o.d ? ' active' : ''), o.t);
      b.onclick = function () {
        if (state.search.days === o.d) return;
        state.search.days = o.d;
        state.search.q = '';       // 强制重新检索（天数变了，缓存 key 不同）
        doSearch(state.q);
      };
      acts.appendChild(b);
    });
    const clear = U.el('button', 'btn btn-xs', '× 清除搜索');
    clear.title = '清除关键词，回到快讯流';
    clear.onclick = clearSearch;
    acts.appendChild(clear);
    bar.appendChild(acts);
    return bar;
  }

  function renderSearchInto(host) {
    const kw = (state.q || '').trim();
    const serverReady = state.search.q === kw && !state.search.loading && !state.search.error;
    const items = serverReady ? (state.search.items || []) : filteredItems();
    host.appendChild(renderSearchHead(kw, serverReady, items.length));

    if (!items.length) {
      const empty = U.el('div', 'empty');
      empty.appendChild(U.el('div', 'empty-icon', state.search.loading ? '⏳' : '🔍'));
      if (state.search.loading) {
        empty.appendChild(U.el('div', 'empty-title', '正在检索…'));
        empty.appendChild(U.el('div', 'empty-desc', '当前快讯流内没有匹配「' + kw + '」的条目，正在向服务端检索。'));
      } else {
        empty.appendChild(U.el('div', 'empty-title', '未检索到相关资讯'));
        empty.appendChild(U.el('div', 'empty-desc',
          '「' + kw + '」在近 ' + state.search.days + ' 天内没有结果，换个关键词或调大回溯天数试试。'));
      }
      host.appendChild(empty);
      return;
    }
    items.forEach(function (it) { host.appendChild(renderItem(it)); });
  }

  function renderListInto(host) {
    host.innerHTML = '';

    // 有关键词 → 走服务端检索视图（与快讯流互斥）
    if ((state.q || '').trim()) {
      renderSearchInto(host);
      return;
    }

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
      empty.appendChild(U.el('div', 'empty-title', '近 ' + (state.meta ? state.meta.window_minutes : state.minutes) + ' 分钟暂无热点'));
      empty.appendChild(U.el('div', 'empty-desc', '当前来源暂无新快讯，稍后会自动刷新。'));
      host.appendChild(empty);
      return;
    }

    // 按时间分组渲染：刚刚 / 5 分钟内 / 更早
    const now = Date.now() / 1000;
    const groups = [
      { key: 'just', label: '刚刚', test: function (ts) { return ts >= now - 120; } },
      { key: '5min', label: '5 分钟内', test: function (ts) { return ts >= now - 300; } },
      { key: 'older', label: '更早', test: function () { return true; } }
    ];
    const buckets = { just: [], '5min': [], older: [] };
    items.forEach(function (it) {
      for (let i = 0; i < groups.length; i++) {
        if (groups[i].test(it.ts)) {
          buckets[groups[i].key].push(it);
          break;
        }
      }
    });
    groups.forEach(function (g) {
      const list = buckets[g.key];
      if (!list.length) return;
      host.appendChild(U.el('div', 'hotspot-group-label', g.label + ' · ' + list.length + ' 条'));
      list.forEach(function (it) { host.appendChild(renderItem(it)); });
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

    // 操作按钮：AI 分析 + 相关股，统一放在右上角
    const actions = U.el('div', 'hotspot-actions');
    const aiBtn = U.el('button', 'hotspot-action hotspot-action-primary', '🤖 AI 分析');
    aiBtn.title = '分析该快讯对行业的影响与关联度最高的股票';
    aiBtn.onclick = function () { openAnalysis(it); };
    actions.appendChild(aiBtn);
    const relBtn = U.el('button', 'hotspot-action');
    const relActive = state.inline.item && state.inline.item.id === it.id;
    relBtn.className = 'hotspot-action' + (relActive ? ' active' : '');
    relBtn.textContent = '相关股';
    relBtn.title = '展开/收起关联个股';
    relBtn.onclick = function (ev) {
      ev.stopPropagation();
      toggleInlineStocks(it);
    };
    actions.appendChild(relBtn);
    body.appendChild(actions);

    // 外链地址过白名单：7x24 快讯源较杂，非 http(s) 地址退化为纯文本标题
    const href = U.safeUrl(it.url);
    if (href) {
      const a = U.el('a', 'hotspot-title', it.title);
      a.href = href;
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

    // 概念标签：点击后过滤同概念资讯
    const tags = it.tags || [];
    if (tags.length) {
      const tagWrap = U.el('div', 'hotspot-tags');
      tags.forEach(function (t) {
        const chip = U.el('button', 'hotspot-tag ' + (HS_SENT_CLASS[t.sentiment] || 'sent-flat'),
          t.name);
        chip.onclick = function (ev) {
          ev.stopPropagation();
          state.sector = state.sector === t.name ? null : t.name;
          render();
        };
        tagWrap.appendChild(chip);
      });
      body.appendChild(tagWrap);
    }

    // 行内关联股面板（展开时渲染）
    if (state.inline.item && state.inline.item.id === it.id) {
      body.appendChild(renderInlineStocks());
    }

    row.appendChild(body);
    return row;
  }

  // 行内关联股：点击「相关股」展开，复用 AI 分析接口但不弹窗。
  async function toggleInlineStocks(item) {
    if (state.inline.item && state.inline.item.id === item.id) {
      state.inline = { item: null, loading: false, data: null, error: null };
      repaintList();
      return;
    }
    state.inline = { item: item, loading: true, data: null, error: null };
    repaintList();
    try {
      const data = await API.hotspotAnalyze(item, false);
      if (data && data.ok === false) throw new Error(data.error || '分析失败');
      state.inline = { item: item, loading: false, data: data, error: null };
    } catch (err) {
      state.inline = { item: item, loading: false, data: null, error: err.message || String(err) };
    }
    if (isCurrent()) repaintList();
  }

  function renderInlineStocks() {
    const wrap = U.el('div', 'hotspot-inline');
    if (state.inline.loading) {
      wrap.appendChild(U.el('div', 'hotspot-inline-loading', '正在分析关联股…'));
      return wrap;
    }
    if (state.inline.error) {
      wrap.appendChild(U.el('div', 'hotspot-inline-err', state.inline.error));
      return wrap;
    }
    const data = state.inline.data || {};
    const stocks = data.stocks || [];
    if (!stocks.length) {
      wrap.appendChild(U.el('div', 'hotspot-inline-empty', '未检索到明确关联个股'));
      return wrap;
    }
    const list = U.el('div', 'hotspot-inline-stocks');
    stocks.slice(0, 5).forEach(function (s) {
      const card = U.el('div', 'hotspot-inline-stock');
      card.onclick = function () {
        location.hash = '#/stock/' + s.code;
      };
      const head = U.el('div', 'hotspot-inline-head');
      head.appendChild(U.el('span', 'hs-stock-name', s.name || s.code));
      head.appendChild(U.el('span', 'hs-stock-code', s.code));
      if (s.board) head.appendChild(U.el('span', 'hs-stock-board', s.board));
      card.appendChild(head);
      const meta = U.el('div', 'hotspot-inline-meta');
      if (U.isNum(s.price)) {
        meta.appendChild(U.el('span', 'hs-stock-price', U.price(s.price)));
      }
      if (U.isNum(s.change_pct)) {
        meta.appendChild(U.el('span', 'hs-stock-chg ' + U.tone(s.change_pct), U.pct(s.change_pct)));
      }
      if (s.reason) {
        meta.appendChild(U.el('span', 'hs-stock-reason', s.reason));
      }
      card.appendChild(meta);
      list.appendChild(card);
    });
    wrap.appendChild(list);
    return wrap;
  }

  // ---------------------------------------------------------- 快讯 AI 分析弹窗
  // 与股票 AI 分析不同：这是「快讯 → 行业影响 + 关联股」的独立分析。
  const HS_SENT_CLASS = { '利好': 'sent-bull', '利空': 'sent-bear', '中性': 'sent-flat' };
  const hsState = { item: null, loading: false };

  function hsModalRoot() { return document.getElementById('modal-root'); }
  function hsModalBody() { return document.getElementById('modal-body'); }
  function hsModalTitle() { return document.getElementById('modal-title'); }
  function hsModalActions() { return document.getElementById('modal-actions'); }

  function hsShow() {
    hsModalRoot().hidden = false;
    document.body.style.overflow = 'hidden';
  }
  function hsClose() {
    hsModalRoot().hidden = true;
    document.body.style.overflow = '';
    hsState.loading = false;
  }

  async function openAnalysis(item) {
    if (hsState.loading) return;
    hsState.item = item;
    hsModalTitle().textContent = '快讯 AI 分析 · 行业影响与关联股';
    hsModalActions().innerHTML = '';
    renderAnalysisLoading();
    hsShow();
    hsState.loading = true;
    try {
      const data = await API.hotspotAnalyze(item, false);
      renderAnalysis(data);
    } catch (err) {
      renderAnalysisError(err.message);
    } finally {
      hsState.loading = false;
    }
  }

  async function reanalyze() {
    if (hsState.loading || !hsState.item) return;
    hsState.loading = true;
    renderAnalysisLoading();
    hsModalActions().innerHTML = '';
    try {
      const data = await API.hotspotAnalyze(hsState.item, true);
      renderAnalysis(data);
    } catch (err) {
      renderAnalysisError(err.message);
    } finally {
      hsState.loading = false;
    }
  }

  function renderAnalysisLoading() {
    hsModalBody().innerHTML =
      '<div class="ai-loading">'
      + '<div class="ai-spinner"></div>'
      + '<div class="ai-loading-text">正在分析该快讯的行业影响…</div>'
      + '<div class="ai-loading-sub">AI 正在判断利好/利空行业并检索关联股票，请稍候</div>'
      + '</div>';
  }

  function renderAnalysisError(msg) {
    hsModalBody().innerHTML =
      '<div class="empty">'
      + '<div class="empty-icon">⚠️</div>'
      + '<div class="empty-title">分析失败</div>'
      + '<div class="empty-desc">' + U.escapeHtml(msg) + '</div>'
      + '</div>';
    const retry = U.el('button', 'btn btn-sm btn-primary', '重试');
    retry.onclick = reanalyze;
    const acts = hsModalActions();
    acts.innerHTML = '';
    acts.appendChild(retry);
  }

  function renderAnalysis(data) {
    // 后端失败时返回 {ok:false, error}（HTTP 200）：必须显式展示错误，
    // 否则会被渲染成「中性 + 无关联股」的成功结果，误导用户
    if (data && data.ok === false) {
      renderAnalysisError((data.error || '分析失败，请重试') + '');
      return;
    }
    const host = hsModalBody();
    host.innerHTML = '';

    // ---- 整体情绪结论
    const sent = data.sentiment || '中性';
    const verdict = U.el('div', 'ai-verdict ' + (HS_SENT_CLASS[sent] || 'sent-flat'));
    const vhead = U.el('div', 'ai-verdict-head');
    vhead.appendChild(U.el('div', 'ai-action', sent));
    vhead.appendChild(U.el('span', 'ai-conf',
      '引擎：' + (data.engine === 'llm' ? 'AI 大模型' : '内置规则引擎')));
    if (data.model) vhead.appendChild(U.el('span', 'ai-conf', '模型：' + data.model));
    verdict.appendChild(vhead);
    if (data.title) verdict.appendChild(U.el('div', 'ai-reason', data.title));
    if (data.summary) {
      const sum = U.el('div', 'ai-position', data.summary);
      sum.style.display = '-webkit-box';
      sum.style.webkitLineClamp = '3';
      sum.style.webkitBoxOrient = 'vertical';
      sum.style.overflow = 'hidden';
      verdict.appendChild(sum);
    }
    host.appendChild(verdict);

    // ---- 行业影响
    const sec = U.el('div', 'ai-section');
    sec.appendChild(U.el('div', 'ai-section-title', '行业影响'));
    const groups = [
      ['利好行业', data.bullish || [], 'bull'],
      ['利空行业', data.bearish || [], 'bear'],
      ['关注行业', data.watch || [], 'flat']
    ];
    let anyIndustry = false;
    groups.forEach(function (g) {
      if (!g[1].length) return;
      anyIndustry = true;
      sec.appendChild(U.el('div', 'ai-item-label', g[0]));
      const wrap = U.el('div', 'hs-industries');
      g[1].forEach(function (x) {
        const chip = U.el('div', 'hs-industry ' + g[2]);
        chip.appendChild(U.el('span', 'hs-industry-name', x.industry || ''));
        if (x.reason) chip.appendChild(U.el('span', 'hs-industry-reason', x.reason));
        wrap.appendChild(chip);
      });
      sec.appendChild(wrap);
    });
    if (!anyIndustry) {
      sec.appendChild(U.el('div', 'ai-item', '未识别到明确的行业影响（快讯或与具体行业无关）'));
    }
    host.appendChild(sec);

    // ---- 关联度最高的股票
    const ssec = U.el('div', 'ai-section');
    ssec.appendChild(U.el('div', 'ai-section-title', '关联度最高的股票'));
    const stocks = data.stocks || [];
    if (stocks.length) {
      const list = U.el('div', 'hs-stocks');
      stocks.forEach(function (s, i) {
        const card = U.el('div', 'hs-stock');
        card.title = '查看 ' + (s.name || '') + ' 详情';
        card.onclick = function () {
          hsClose();
          location.hash = '#/stock/' + s.code;
        };
        const head = U.el('div', 'hs-stock-head');
        head.appendChild(U.el('span', 'hs-stock-rank', String(i + 1)));
        head.appendChild(U.el('span', 'hs-stock-name', s.name || s.code));
        head.appendChild(U.el('span', 'hs-stock-code', s.code));
        if (s.board) head.appendChild(U.el('span', 'hs-stock-board', s.board));
        const watchBtn = U.el('button', 'btn btn-sm hs-stock-watch' + (s.watched ? '' : ' btn-primary'),
          s.watched ? '已自选' : '加入自选');
        watchBtn.disabled = !!s.watched;
        watchBtn.title = s.watched ? '' : '将 ' + (s.name || s.code) + ' 加入自选';
        watchBtn.onclick = function (ev) {
          ev.stopPropagation(); // 只入库，不触发卡片跳转详情页
          hsAddWatch(s, watchBtn);
        };
        head.appendChild(watchBtn);
        card.appendChild(head);
        const meta = U.el('div', 'hs-stock-meta');
        if (U.isNum(s.price)) {
          meta.appendChild(U.el('span', 'hs-stock-price', U.price(s.price)));
        }
        if (U.isNum(s.change_pct)) {
          meta.appendChild(U.el('span', 'hs-stock-chg ' + U.tone(s.change_pct), U.pct(s.change_pct)));
        }
        // 关联命中明细：每个检索词一个 chip，悬停显示检索来源（旧缓存无 matches 时回退文本）
        if (s.matches && s.matches.length) {
          const kwWrap = U.el('div', 'hs-stock-kws');
          s.matches.forEach(function (m) {
            const chip = U.el('span', 'hs-stock-kw', m.keyword || '');
            chip.title = '检索词「' + (m.keyword || '') + '」命中'
              + (m.source ? ' · 来源 ' + m.source : '');
            kwWrap.appendChild(chip);
          });
          meta.appendChild(kwWrap);
        } else if (s.reason) {
          meta.appendChild(U.el('span', 'hs-stock-reason', '关联：' + s.reason));
        }
        card.appendChild(meta);
        list.appendChild(card);
      });
      ssec.appendChild(list);
    } else {
      ssec.appendChild(U.el('div', 'ai-item', '未检索到明确的关联个股（检索源暂不可用或快讯无个股关联）'));
    }
    host.appendChild(ssec);

    // ---- 元信息
    const meta = U.el('div', 'ai-meta');
    meta.appendChild(U.el('span', '',
      '引擎：' + (data.engine === 'llm' ? 'AI 大模型' : '内置规则引擎')));
    if (data.model) meta.appendChild(U.el('span', '', '模型：' + data.model));
    if (data.source) meta.appendChild(U.el('span', '', '来源：' + data.source));
    meta.appendChild(U.el('span', '', '生成时间：' + (data.fetched_at || '--')));
    host.appendChild(meta);

    host.appendChild(U.el('div', 'ai-disclaimer',
      '本分析基于公开快讯与行情数据由程序自动生成，不构成投资建议。市场有风险，决策请自行判断。'));

    // ---- 操作按钮
    const acts = hsModalActions();
    acts.innerHTML = '';
    const again = U.el('button', 'btn btn-sm btn-primary', '重新分析');
    again.onclick = reanalyze;
    acts.appendChild(again);
  }

  async function hsAddWatch(s, btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add('loading');
    try {
      await API.addWatch(s.code, s.name, s.board);
      btn.classList.remove('loading', 'btn-primary');
      btn.textContent = '已自选';
      btn.title = '';
      U.toast('已添加「' + (s.name || s.code) + '」到自选', 'ok');
    } catch (err) {
      btn.disabled = false;
      btn.classList.remove('loading');
      U.toast('添加失败：' + err.message, 'err');
    }
  }

  // 关闭交互：与 AI/资讯弹窗共用 modal-root 与 data-close 监听
  document.addEventListener('click', function (e) {
    if (e.target && e.target.getAttribute && e.target.getAttribute('data-close') === '1') hsClose();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !hsModalRoot().hidden) hsClose();
  });

  async function load(force) {
    if (state.loading) return;
    state.loading = true;
    if (isCurrent() && !state.items.length) render();
    try {
      const data = await API.hotspot(state.minutes, force);
      state.items = data.items || [];
      state.meta = data.meta || null;
      state.error = (data.meta && data.meta.error) || null;
      // 当前筛选来源已无条目时回退到「全部」，避免空列表误导
      if (state.filter !== 'all' && !state.items.some(function (it) { return it.origin === state.filter; })) {
        state.filter = 'all';
      }
      // 当前选中概念在新窗口里不存在时，清除概念过滤
      if (state.sector) {
        const heat = (state.meta && state.meta.sector_heat) || [];
        if (!heat.some(function (s) { return s.name === state.sector; })) {
          state.sector = null;
        }
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

  export const PageHotspot = {
    mount: function () {
      state.filter = 'all';
      state.sector = null;
      state.inline = { item: null, loading: false, data: null, error: null };
      state.q = '';
      state.search.q = '';
      state.search.items = [];
      state.search.meta = null;
      state.search.error = null;
      state.search.loading = false;
      state.error = null;
      render();
      return load(false);
    },
    refresh: function () { return load(true); },
    tick: function () {
      // 正在搜索时不自动刷新：整页重绘会顶掉搜索框焦点与光标
      if ((state.q || '').trim()) return;
      // 自动刷新节流到 60s：后端聚合结果本身有 90s 缓存，无需每 5s 打一次
      if (Date.now() - state.lastAuto < 60000) return;
      load(false);
    }
  };
