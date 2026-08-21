/* 热点追踪页：近 30 分钟市场热点（同花顺/东方财富/新浪财经 7x24 快讯聚合） */
(function (global) {
  'use strict';

  const state = {
    items: [],
    meta: null,
    filter: 'all',
    q: '',
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
    const q = (state.q || '').toLowerCase();
    return state.items.filter(function (it) {
      if (state.filter !== 'all' && it.origin !== state.filter) return false;
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
    // 关键词搜索：只重建列表，不打断页面滚动
    const search = U.el('input', 'hotspot-search');
    search.type = 'text';
    search.placeholder = '搜索快讯…';
    search.value = state.q;
    search.setAttribute('aria-label', '搜索快讯');
    search.oninput = function () {
      state.q = search.value;
      const host = document.getElementById('hotspot-list');
      if (host) renderListInto(host);
    };
    bar.appendChild(search);
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
      empty.appendChild(U.el('div', 'empty-icon', state.q ? '🔍' : '🕐'));
      if (state.q) {
        empty.appendChild(U.el('div', 'empty-title', '未找到相关快讯'));
        empty.appendChild(U.el('div', 'empty-desc', '没有标题/摘要/来源包含「' + state.q + '」的条目，换个关键词试试。'));
      } else {
        empty.appendChild(U.el('div', 'empty-title', '近 ' + (state.meta ? state.meta.window_minutes : 30) + ' 分钟暂无热点'));
        empty.appendChild(U.el('div', 'empty-desc', '当前来源暂无新快讯，稍后会自动刷新。'));
      }
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

    // 操作区：AI 分析（分析该快讯利好/利空哪些行业 + 关联度最高的股票）
    const acts = U.el('div', 'hotspot-item-actions');
    const aiBtn = U.el('button', 'btn btn-sm btn-primary', '🤖 AI 分析');
    aiBtn.title = '分析该快讯对行业的影响与关联度最高的股票';
    aiBtn.onclick = function () { openAnalysis(it); };
    acts.appendChild(aiBtn);
    body.appendChild(acts);

    row.appendChild(body);
    return row;
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
        if (s.reason) meta.appendChild(U.el('span', 'hs-stock-reason', '关联：' + s.reason));
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
      state.q = '';
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
