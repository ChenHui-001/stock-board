/* 热点追踪页：近 30 分钟市场热点（同花顺/东方财富/新浪财经 7x24 快讯聚合） */
(function (global) {
  'use strict';

  const state = {
    items: [],
    meta: null,
    filter: 'all',
    q: '',
    minutes: 30,        // 快讯时间窗（分钟），可切 15/30/60
    loading: false,
    error: null,
    lastAuto: 0,
    view: 'feed',       // feed=快讯列表 / value=价值投资选股
    // 服务端关键词检索（真搜索，不是过滤当前页）
    search: {
      q: '',            // 已发起检索的关键词，用于丢弃过期响应
      days: 7,
      items: [],
      meta: null,
      loading: false,
      error: null
    },
    value: null,        // 价值选股结果
    valueLoading: false,
    valueError: null,
    weights: null,      // 价值选股各维度权重
    weightsOpen: false  // 权重配置表单是否展开
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
    if (state.view === 'value') {
      card.appendChild(renderValuePanel());
    } else {
      card.appendChild(renderFilters());
      const listHost = U.el('div', 'hotspot-list');
      listHost.id = 'hotspot-list';
      card.appendChild(listHost);
      renderListInto(listHost);
    }
    root.appendChild(card);
  }

  // ---------------------------------------------------------- 价值投资选股
  // 右侧「价值投资」菜单：A股快速轮动量化选股（市场环境 + 板块强度 + 多维评分 + 分级池）
  function renderHeadTabs() {
    const tabs = U.el('div', 'hotspot-tabs');
    [
      { key: 'feed', label: '快讯' },
      { key: 'value', label: '💎 价值投资' }
    ].forEach(function (t) {
      const btn = U.el('button', 'tab' + (state.view === t.key ? ' active' : ''), t.label);
      btn.onclick = function () {
        state.view = t.key;
        render();
      };
      tabs.appendChild(btn);
    });
    return tabs;
  }

  async function loadValue(force) {
    if (state.valueLoading) return;
    state.valueLoading = true;
    state.valueError = null;
    if (isCurrent()) render();
    try {
      state.value = await API.valueScreen(force);
      if (state.value && state.value.weights) state.weights = state.value.weights;
    } catch (err) {
      state.valueError = err.message || String(err);
      U.toast('价值选股失败：' + state.valueError, 'err');
    } finally {
      state.valueLoading = false;
      if (isCurrent()) render();
    }
  }

  function valueGradeClass(g) {
    return { S: 'vg-s', A: 'vg-a', B: 'vg-b', C: 'vg-c', D: 'vg-d' }[g] || 'vg-d';
  }

  function valueSignalClass(sig) {
    if (sig === 'BUY' || sig === 'BREAKOUT_BUY' || sig === 'PULLBACK_BUY') return 'vs-buy';
    if (sig === 'WATCH') return 'vs-watch';
    if (sig === 'REDUCE') return 'vs-reduce';
    return 'vs-avoid';
  }

  // 选股结果行的「加入自选」按钮：只入库，不触发行点击跳详情
  function valueWatchCell(s) {
    const td = U.el('td', 'value-td-watch');
    const btn = U.el('button', 'btn btn-xs' + (s.watched ? '' : ' btn-primary'),
      s.watched ? '已自选' : '加入自选');
    btn.disabled = !!s.watched;
    btn.title = s.watched ? '' : '将 ' + (s.name || s.code) + ' 加入自选';
    btn.onclick = function (ev) {
      ev.stopPropagation();
      hsAddWatch(s, btn);
    };
    td.appendChild(btn);
    return td;
  }

  // 权重配置：未加载过时取一次（轻量 GET，不触发选股）
  function ensureValueWeights() {
    if (state.weights) return;
    API.valueWeights().then(function (res) {
      state.weights = res;
      if (isCurrent() && state.view === 'value') render();
    }).catch(function () { /* 权重读取失败不阻塞面板 */ });
  }

  // 各维度权重表单（结果面板与空态共用）
  function renderValueWeightsForm() {
    const w = state.weights || {};
    const sec = U.el('div', 'value-weights' + (state.weightsOpen ? '' : ' hidden'));
    sec.appendChild(U.el('div', 'value-sec-title', '⚖ 评分权重配置'));
    sec.appendChild(U.el('div', 'value-weights-sub',
      '相对权重（0.2~3.0，默认 1.0）：调大某维度，该维度强的股票总分上升、弱的下降（总分口径恒 0~92）；'
      + '保存后自动重新选股（缓存作废）。'));
    const grid = U.el('div', 'value-weights-grid');
    const mx = (state.weights && state.weights.maxes) || {};
    const base = (state.weights && state.weights.base_total) || 92;
    function share(k) {
      const m = mx[k];
      if (!m) return '';
      return '（满分 ' + m + ' · 默认占 ' + Math.round(m / base * 100) + '%）';
    }
    [
      ['finance', '基本面'],
      ['board', '板块'],
      ['flow', '资金'],
      ['volume', '量价筹码'],
      ['emotion', '情绪妖股']
    ].forEach(function (r) {
      const row = U.el('label', 'value-weights-row');
      row.appendChild(U.el('span', 'value-weights-label', r[1] + share(r[0])));
      const inp = U.el('input', 'value-weights-input');
      inp.type = 'number';
      inp.min = 0.2;
      inp.max = 3.0;
      inp.step = 0.1;
      inp.value = w[r[0]] != null ? w[r[0]] : 1.0;
      inp.dataset.k = r[0];
      row.appendChild(inp);
      grid.appendChild(row);
    });
    sec.appendChild(grid);
    const btns = U.el('div', 'value-weights-actions');
    const saveBtn = U.el('button', 'btn btn-sm btn-primary', '保存权重');
    saveBtn.onclick = function () { saveValueWeights(); };
    btns.appendChild(saveBtn);
    const resetBtn = U.el('button', 'btn btn-sm', '恢复默认');
    resetBtn.onclick = function () { resetValueWeights(); };
    btns.appendChild(resetBtn);
    btns.appendChild(U.el('span', 'value-weights-note', '保存后自动重新选股'));
    sec.appendChild(btns);
    return sec;
  }

  async function saveValueWeights() {
    const body = {};
    document.querySelectorAll('.value-weights-input').forEach(function (inp) {
      body[inp.dataset.k] = parseFloat(inp.value);
    });
    try {
      const res = await API.valueWeightsSave(body);
      state.weights = res;
      U.toast('权重已保存，正在重新选股…', 'ok');
      loadValue(true);
    } catch (err) {
      U.toast('保存权重失败：' + err.message, 'err');
    }
  }

  async function resetValueWeights() {
    if (!confirm('恢复价值选股权重为默认（全 1.0）？')) return;
    try {
      const res = await API.valueWeightsReset();
      state.weights = res;
      U.toast('权重已恢复默认，正在重新选股…', 'ok');
      loadValue(true);
    } catch (err) {
      U.toast('恢复权重失败：' + err.message, 'err');
    }
  }

  function renderValuePanel() {
    const wrap = U.el('div', 'value-panel');
    ensureValueWeights();
    if (state.valueLoading && !state.value) {
      wrap.appendChild(U.el('div', 'loading-block', '正在运行价值选股（市场环境 → 板块 → 候选 → 多维评分）…'));
      return wrap;
    }
    if (state.valueError && !state.value) {
      const empty = U.el('div', 'empty');
      empty.appendChild(U.el('div', 'empty-icon', '⚠️'));
      empty.appendChild(U.el('div', 'empty-title', '价值选股暂不可用'));
      empty.appendChild(U.el('div', 'empty-desc', state.valueError));
      wrap.appendChild(empty);
      return wrap;
    }
    if (!state.value) {
      // 手动执行：切页不自动选股，由用户点「开始选股」触发
      const empty = U.el('div', 'empty value-empty');
      empty.appendChild(U.el('div', 'empty-icon', '💎'));
      empty.appendChild(U.el('div', 'empty-title', '尚未运行选股'));
      empty.appendChild(U.el('div', 'empty-desc', '点击「开始选股」手动运行：市场环境 → 板块 → 候选 → 多维评分 → 分级池（约 10~20 秒）。可先展开「⚖ 权重配置」调整各维度权重。'));
      const btn = U.el('button', 'btn btn-primary', '▶ 开始选股');
      btn.disabled = state.valueLoading;
      btn.onclick = function () { loadValue(true); };
      empty.appendChild(btn);
      const wtBtn = U.el('button', 'btn btn-sm' + (state.weightsOpen ? ' active' : ''), '⚖ 权重配置');
      wtBtn.onclick = function () {
        state.weightsOpen = !state.weightsOpen;
        const form = wrap.querySelector('.value-weights');
        if (form) form.classList.toggle('hidden', !state.weightsOpen);
      };
      empty.appendChild(wtBtn);
      wrap.appendChild(empty);
      wrap.appendChild(renderValueWeightsForm());
      return wrap;
    }
    const d = state.value;

    // ---- 市场状态
    const m = d.market || {};
    const mk = U.el('div', 'value-market');
    mk.appendChild(U.el('div', 'value-mk-title',
      '市场状态：' + (m.name || '--') + '（' + (m.state || '--') + '）'));
    const mkMeta = U.el('div', 'value-mk-meta');
    mkMeta.appendChild(U.el('span', 'value-mk-item', '进攻等级 ' + (m.attack ?? '--') + '/100'));
    mkMeta.appendChild(U.el('span', 'value-mk-item', '情绪 ' + (m.emotion || '--')));
    mkMeta.appendChild(U.el('span', 'value-mk-item', '涨停 ' + (m.zt_count ?? '--') + ' 只'));
    mkMeta.appendChild(U.el('span', 'value-mk-item', '候选 ' + (m.candidate_count ?? '--') + ' 只'));
    (m.indices || []).slice(0, 5).forEach(function (ix) {
      const cls = U.tone(ix.change_pct);
      mkMeta.appendChild(U.el('span', 'value-mk-item ' + cls,
        ix.name + ' ' + (U.isNum(ix.change_pct) ? U.pct(ix.change_pct) : '--')));
    });
    mk.appendChild(mkMeta);
    wrap.appendChild(mk);

    // ---- 最强板块
    const boards = d.board_top || [];
    if (boards.length) {
      const bsec = U.el('div', 'value-section');
      bsec.appendChild(U.el('div', 'value-sec-title', '🔥 最强板块 TOP' + boards.length));
      const bwrap = U.el('div', 'value-boards');
      boards.forEach(function (b, i) {
        const chip = U.el('div', 'value-board');
        chip.appendChild(U.el('span', 'value-board-rank', String(i + 1)));
        chip.appendChild(U.el('span', 'value-board-name', b.name));
        chip.appendChild(U.el('span', 'value-board-str', '强度 ' + (b.strength ?? '--')));
        bwrap.appendChild(chip);
      });
      bsec.appendChild(bwrap);
      wrap.appendChild(bsec);
    }

    // ---- 三个分级池
    const pools = d.pools || {};
    const poolDefs = [
      { key: 'core', title: '核心价值成长池', sub: '基本面+板块+资金综合最优（适合 1~12 个月）' },
      { key: 'trend', title: '趋势波段池', sub: '板块+资金+量价+情绪（适合 5~30 个交易日）' },
      { key: 'emotion', title: '情绪妖股池', sub: '连板高度+换手+情绪（适合 1~10 个交易日，高风险）' }
    ];
    poolDefs.forEach(function (pd) {
      const list = pools[pd.key] || [];
      if (!list.length) return;
      const sec = U.el('div', 'value-section');
      const head = U.el('div', 'value-sec-head');
      head.appendChild(U.el('div', 'value-sec-title', pd.title));
      head.appendChild(U.el('span', 'value-sec-sub', pd.sub));
      sec.appendChild(head);
      const table = U.el('table', 'value-table');
      const thead = U.el('thead');
      const tr = U.el('tr');
      ['#', '股票', '板块', '总分', '基本面', '资金', '买点', '信号', '等级', '操作'].forEach(function (h) {
        tr.appendChild(U.el('th', '', h));
      });
      thead.appendChild(tr);
      table.appendChild(thead);
      const tbody = U.el('tbody');
      list.forEach(function (s, i) {
        const row = U.el('tr', 'value-row');
        row.title = s.name + ' ' + s.code + ' · 板块 ' + (s.board || '--');
        row.onclick = function () { location.hash = '#/stock/' + s.code; };
        row.appendChild(U.el('td', 'value-td-rank', String(i + 1)));
        const tdName = U.el('td', 'value-td-name');
        tdName.appendChild(U.el('span', '', s.name || s.code));
        tdName.appendChild(U.el('span', 'value-td-code', s.code));
        row.appendChild(tdName);
        row.appendChild(U.el('td', 'value-td-board', s.board || '--'));
        row.appendChild(U.el('td', 'value-td-score', String(s.total_score ?? '--')));
        row.appendChild(U.el('td', 'value-td-sub', String((s.scores && s.scores.finance) ?? '--')));
        row.appendChild(U.el('td', 'value-td-sub', String((s.scores && s.scores.flow) ?? '--')));
        row.appendChild(U.el('td', 'value-td-sub', String(s.buy_score ?? '--')));
        row.appendChild(U.el('td', 'value-td-signal ' + valueSignalClass(s.signal), s.signal || '--'));
        const gradeTd = U.el('td', 'value-td-grade');
        gradeTd.appendChild(U.el('span', 'value-grade ' + valueGradeClass(s.grade), s.grade || '--'));
        row.appendChild(gradeTd);
        row.appendChild(valueWatchCell(s));
        tbody.appendChild(row);
      });
      table.appendChild(tbody);
      sec.appendChild(table);
      wrap.appendChild(sec);
    });

    // ---- 完整结果（含风险/完整度/评分明细）
    const all = d.stocks || [];
    if (all.length) {
      const sec = U.el('div', 'value-section');
      sec.appendChild(U.el('div', 'value-sec-title', '📋 全部候选明细（' + all.length + ' 只）'));
      const table = U.el('table', 'value-table value-table-wide');
      const thead = U.el('thead');
      const tr = U.el('tr');
      ['排名', '股票', '板块', '总分', '基', '板', '资', '量价', '情绪', '风险', '买点', '交易', '完整度', '等级', '信号', '风险提示', '操作'].forEach(function (h) {
        tr.appendChild(U.el('th', '', h));
      });
      thead.appendChild(tr);
      table.appendChild(thead);
      const tbody = U.el('tbody');
      all.forEach(function (s, i) {
        const row = U.el('tr', 'value-row');
        row.onclick = function () { location.hash = '#/stock/' + s.code; };
        row.appendChild(U.el('td', '', String(i + 1)));
        const tdName = U.el('td', 'value-td-name');
        tdName.appendChild(U.el('span', '', s.name || s.code));
        tdName.appendChild(U.el('span', 'value-td-code', s.code));
        row.appendChild(tdName);
        row.appendChild(U.el('td', '', s.board || '--'));
        row.appendChild(U.el('td', 'value-td-score', String(s.total_score ?? '--')));
        const sc = s.scores || {};
        row.appendChild(U.el('td', '', String(sc.finance ?? '--')));
        row.appendChild(U.el('td', '', String(sc.board ?? '--')));
        row.appendChild(U.el('td', '', String(sc.flow ?? '--')));
        row.appendChild(U.el('td', '', String(sc.volume ?? '--')));
        row.appendChild(U.el('td', '', String(sc.emotion ?? '--')));
        row.appendChild(U.el('td', 'value-td-risk' + ((s.risk_notes && s.risk_notes.length) ? ' has' : ''),
          String(sc.risk ?? '--')));
        row.appendChild(U.el('td', '', String(s.buy_score ?? '--')));
        row.appendChild(U.el('td', '', String(s.trade_score ?? '--')));
        row.appendChild(U.el('td', '', (s.completeness ?? '--') + '%'));
        row.appendChild(U.el('td', 'value-td-grade',
          s.grade + ' · ' + (s.grade_name || '')));
        row.appendChild(U.el('td', 'value-td-signal ' + valueSignalClass(s.signal), s.signal || '--'));
        row.appendChild(U.el('td', 'value-td-notes', (s.risk_notes || []).join('、') || '--'));
        row.appendChild(valueWatchCell(s));
        tbody.appendChild(row);
      });
      table.appendChild(tbody);
      sec.appendChild(table);
      wrap.appendChild(sec);
    }

    // ---- 权重配置 + 刷新 + 元信息
    wrap.appendChild(renderValueWeightsForm());
    const foot = U.el('div', 'value-foot');
    const wtBtn = U.el('button', 'btn btn-sm' + (state.weightsOpen ? ' active' : ''), '⚖ 权重配置');
    wtBtn.onclick = function () {
      state.weightsOpen = !state.weightsOpen;
      const form = wrap.querySelector('.value-weights');
      if (form) form.classList.toggle('hidden', !state.weightsOpen);
    };
    foot.appendChild(wtBtn);
    const refresh = U.el('button', 'btn btn-sm', '⟳ 重新选股');
    refresh.disabled = state.valueLoading;
    refresh.onclick = function () { loadValue(true); };
    foot.appendChild(refresh);
    foot.appendChild(U.el('span', 'value-foot-meta',
      '生成时间：' + (d.generated_at || '--') + ' · 数据来自腾讯/东财/同花顺公开接口'));
    foot.appendChild(U.el('div', 'value-disclaimer',
      '以上由程序按量化规则自动生成，不构成投资建议。市场有风险，决策请自行判断。'));
    wrap.appendChild(foot);
    return wrap;
  }

  function renderHead() {
    const head = U.el('div', 'card-head hotspot-head');
    const left = U.el('div');
    left.appendChild(U.el('div', 'card-title', '🔥 热点追踪'));
    const sub = U.el('div', 'card-sub');
    if (state.view === 'value') {
      sub.textContent = state.value
        ? 'A股快速轮动量化选股 · 市场环境 → 板块 → 多维评分 → 分级池'
        : (state.valueLoading ? '正在运行量化选股引擎…' : '量化选股面板');
    } else if (state.error) {
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

    head.appendChild(renderHeadTabs());

    const refresh = U.el('button', 'btn btn-sm', '⟳ 刷新');
    refresh.onclick = function () {
      if (state.view === 'value') loadValue(true); else load(true);
    };
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
        repaintList();
      };
      bar.appendChild(chip);
    });

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
    bar.appendChild(range);

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
    bar.appendChild(search);
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
    // AI 分析按钮移到源行末尾（margin-left: auto 由 CSS 推到最右），与「内容出处」语义关联，
    // 不再占据卡片底部独立行；弹窗交互保留不变。
    const aiBtn = U.el('button', 'btn btn-sm btn-primary hotspot-ai-btn', '🤖 AI 分析');
    aiBtn.title = '分析该快讯对行业的影响与关联度最高的股票';
    aiBtn.onclick = function () { openAnalysis(it); };
    metaLine.appendChild(aiBtn);
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
})(window);
