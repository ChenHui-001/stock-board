/* 路由与全局调度 */
(function (global) {
  'use strict';

  const state = { route: null, param: null, timer: null, session: null, meta: null };

  function parseHash() {
    const hash = (location.hash || '#/home').replace(/^#\/?/, '');
    const parts = hash.split('/').filter(Boolean);
    if (!parts.length) return { route: 'home', param: null };
    if (parts[0] === 'stock' && parts[1]) return { route: 'stock', param: parts[1] };
    if (parts[0] === 'search') return { route: 'search', param: null };
    return { route: 'home', param: null };
  }

  function currentPage() {
    if (state.route === 'search') return PageSearch;
    if (state.route === 'stock') return PageDetail;
    return PageHome;
  }

  function setNavActive() {
    document.querySelectorAll('.nav-item').forEach(function (node) {
      const r = node.getAttribute('data-route');
      const on = (r === 'home' && (state.route === 'home' || state.route === 'stock'))
        || (r === state.route);
      node.classList.toggle('active', on);
    });
  }

  async function route() {
    const next = parseHash();
    state.route = next.route;
    state.param = next.param;
    setNavActive();
    Charts.disposeAll();

    const manageBtn = document.getElementById('btn-manage');
    manageBtn.style.display = state.route === 'home' ? '' : 'none';

    if (state.route === 'search') {
      PageSearch.mount();
    } else if (state.route === 'stock') {
      await PageDetail.mount(state.param);
    } else {
      await PageHome.mount();
    }
    restartTimer();
  }

  function setSession(session) {
    if (!session) return;
    const changed = !state.session || state.session.trading !== session.trading;
    state.session = session;
    const badge = document.getElementById('session-badge');
    badge.innerHTML = session.label + (session.trading
      ? '<i class="session-hint"> · 3秒自动刷新</i>'
      : '<i class="session-hint"> · 手动刷新</i>');
    badge.classList.toggle('live', !!session.trading);
    if (changed) restartTimer();
  }

  function restartTimer() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
    const interval = state.session && state.session.interval_ms;
    if (!interval) return;              // 盘后不自动刷新（需求 2.1）
    if (state.route === 'search') return;
    state.timer = setInterval(function () {
      if (document.hidden) return;      // 页面不可见时暂停，省流量也省数据源配额
      currentPage().tick();
    }, interval);
  }

  async function manualRefresh() {
    const btn = document.getElementById('btn-refresh');
    btn.classList.add('spinning');
    btn.disabled = true;
    try {
      await currentPage().refresh();
      U.toast('数据已刷新', 'ok');
    } catch (err) {
      U.toast('刷新失败：' + err.message, 'err');
    } finally {
      btn.classList.remove('spinning');
      btn.disabled = false;
    }
  }

  async function loadMeta() {
    try {
      state.meta = await API.meta();
      setSession(state.meta.session);
      renderSubBar();
      renderDsBadge();
      if (!document.getElementById('ds-popover').hidden) renderDsPopover();
    } catch (err) {
      document.getElementById('session-badge').textContent = '后端未就绪';
    }
  }

  // 数据源能力的中文短标签
  const CAP_LABELS = {
    quotes: '行情', kline: 'K线', fund_flow: '资金', margin: '两融',
    search: '搜索', hot: '热门', boards: '板块', industry: '行业'
  };

  function providerHealth(p) {
    // 返回 'ok' / 'warn' / 'err'：有冷却或连续失败即降级
    if (p.cooling > 0) return 'err';
    if (p.fails > 0) return 'warn';
    return 'ok';
  }

  function renderDsBadge() {
    const badge = document.getElementById('btn-ds');
    const dot = document.getElementById('ds-dot');
    const label = document.getElementById('ds-label');
    if (!badge || !state.meta) return;
    const providers = state.meta.providers || [];
    const throttled = state.meta.throttled_hosts || {};
    const tkeys = Object.keys(throttled);
    const errs = providers.filter(function (p) { return providerHealth(p) === 'err'; });
    const warns = providers.filter(function (p) { return providerHealth(p) === 'warn'; });
    const unhealthy = errs.length + (warns.length ? 1 : 0) + (tkeys.length ? 1 : 0);

    let cls = 'ok';
    if (errs.length || !providers.length) cls = 'err';
    else if (unhealthy) cls = 'warn';
    badge.className = 'ds-badge ' + cls;
    if (!providers.length) {
      label.textContent = '数据源不可用';
      dot.className = 'ds-dot';
      return;
    }
    const okCount = providers.filter(function (p) { return providerHealth(p) === 'ok'; }).length;
    label.textContent = '数据源 ' + okCount + '/' + providers.length;
    dot.className = 'ds-dot';
    badge.title = tkeys.length
      ? '数据源状态：' + okCount + '/' + providers.length + ' 正常，限流中：' + tkeys.join('、')
      : '数据源状态：' + okCount + '/' + providers.length + ' 正常';
  }

  // ------------------------------------------------------------ 数据源状态面板
  const dsState = { view: 'meta', checking: false, lastResult: null };

  // 自检结果里每个能力的状态摘要（供面板展示）
  function capSummary(cap, res) {
    if (!res || !res.ok) {
      const err = (res && res.error) || '未知错误';
      return { ok: false, text: CAP_LABELS[cap] || cap, title: err };
    }
    if (cap === 'quotes') {
      const sample = res.count + '/' + (dsState.lastSample ? dsState.lastSample.length : '?');
      const dates = (res.trade_dates || []).join(',');
      return { ok: true, text: CAP_LABELS[cap] + ' ' + sample, title: dates ? '行情日期：' + dates : '行情正常' };
    }
    if (cap === 'kline') {
      const flag = res.stale_flag ? ' ⚠延迟' : '';
      return { ok: true, text: CAP_LABELS[cap] + ' ' + (res.bars || 0) + '根', title: res.first_date + ' ~ ' + res.last_date + flag };
    }
    if (cap === 'fund_flow') {
      return { ok: true, text: CAP_LABELS[cap] + '→' + (res.last_date || '--'), title: '主力净额：' + res.last_main + (res.tiered ? '（四档）' : '（两档）') };
    }
    if (cap === 'margin') {
      return { ok: true, text: CAP_LABELS[cap] + '→' + (res.last_date || '--'), title: '共 ' + (res.rows || 0) + ' 日' };
    }
    if (cap === 'search') {
      return { ok: true, text: CAP_LABELS[cap] + ' ' + (res.count || 0) + '条', title: res.first || '' };
    }
    if (cap === 'hot') {
      const c = res.counts || {};
      return { ok: true, text: CAP_LABELS[cap] + ' ' + Object.keys(c).length + '榜', title: JSON.stringify(c) };
    }
    return { ok: true, text: CAP_LABELS[cap] || cap, title: '' };
  }

  function renderDsCheckLoading() {
    const pop = document.getElementById('ds-popover');
    pop.innerHTML = '<div class="ds-head">🔍 数据源自检</div>' +
      '<div class="ds-checking"><div class="ds-spinner"></div>' +
      '正在逐源实测行情/K线/资金/两融/搜索/热门…<br>' +
      '<span class="ds-faint">约 10-30 秒，期间会真实请求数据源</span></div>' +
      '<div class="ds-actions"><button class="btn btn-ghost" id="ds-back">返回</button></div>';
    bindDsActions();
  }

  function renderDsCheckResult(result) {
    const pop = document.getElementById('ds-popover');
    const parts = [];
    dsState.lastSample = result.sample || [];
    parts.push('<div class="ds-head">🔍 数据源自检 <span class="ds-faint">' +
      (result.time || '').slice(11, 19) + '</span></div>');
    (result.providers || []).forEach(function (p) {
      const okCaps = Object.keys(p.results).filter(function (c) { return p.results[c].ok; });
      const allCaps = Object.keys(p.results);
      const cls = okCaps.length === allCaps.length && allCaps.length ? 'ok' : (okCaps.length ? 'warn' : 'err');
      parts.push('<div class="ds-row ds-check-row">' +
        '<span class="ds-name ' + cls + '">' + U.escapeHtml(p.name) + '</span>' +
        '<span class="ds-caps">' +
        Object.keys(p.results).map(function (c) {
          const s = capSummary(c, p.results[c]);
          return '<span class="ds-cap ' + (s.ok ? 'ok' : 'err') + '" title="' + U.escapeHtml(s.title || s.text) + '">' +
            U.escapeHtml(s.text) + '</span>';
        }).join('') +
        '</span></div>');
    });
    parts.push('<div class="ds-sub">行情源可用：' + (result.quote_sources_ok || []).length +
      ' 个' + ((result.quote_sources_ok || []).length ? '（' + result.quote_sources_ok.join('、') + '）' : '') +
      '<br>最新行情日期：' + (result.latest_trade_date || '--'));

    // ---- 盘口信号近期命中率（与回测脚本同口径）
    const bt = result.backtest;
    if (bt && bt.ok) {
      const btParts = [];
      btParts.push('<div class="ds-bt-head">盘口信号近期命中率 <span class="ds-faint">' +
        bt.samples + ' 样本 / ' + bt.stocks + ' 只 · 基线 ' + bt.base_up_rate + '%</span></div>');
      // 分桶：vs基线>0 绿色、<0 红色
      const buckRow = bt.buckets || [];
      if (buckRow.length) {
        btParts.push('<div class="ds-bt-buckets">' + buckRow.map(function (b) {
          const cls = b.vs_base >= 2 ? 'up' : (b.vs_base <= -2 ? 'down' : 'flat');
          return '<span class="ds-bt-bucket ' + cls + '" title="样本 ' + b.n + ' · 平均涨跌 ' + b.avg_ret + '%">' +
            U.escapeHtml(b.bucket) + ' ' + b.up_rate + '%</span>';
        }).join('') + '</div>');
      }
      // 有效/反向信号摘要（样本≥50 才提示）
      const solid = (bt.signals || []).filter(function (s) { return s.n >= 50; });
      const strong = solid.filter(function (s) {
        return s.advice.indexOf('上调') >= 0 || s.advice.indexOf('维持') >= 0;
      });
      const weak = solid.filter(function (s) { return s.advice.indexOf('下调') >= 0 || s.advice.indexOf('反向') >= 0; });
      if (strong.length) {
        btParts.push('<div class="ds-bt-note ok">有效：' +
          strong.map(function (s) { return U.escapeHtml(s.signal) + ' ' + s.hit_rate + '%'; }).join('、') + '</div>');
      }
      if (weak.length) {
        btParts.push('<div class="ds-bt-note warn">偏弱/反向：' +
          weak.map(function (s) { return U.escapeHtml(s.signal) + ' ' + s.hit_rate + '%'; }).join('、') + '</div>');
      }
      btParts.push('<div class="ds-bt-detail" style="display:none">');
      btParts.push((bt.signals || []).map(function (s) {
        return '<div class="ds-bt-row"><span>' + U.escapeHtml(s.signal) +
          (s.bullish ? ' 看多' : ' 看空') + '</span><span>n=' + s.n + '</span>' +
          '<span class="' + (s.hit_rate >= bt.base_up_rate + 2 ? 'up' : s.hit_rate <= bt.base_up_rate - 2 ? 'down' : '') + '">命中 ' + s.hit_rate + '%</span>' +
          '<span class="ds-faint">' + U.escapeHtml(s.advice) + '</span></div>';
      }).join(''));
      btParts.push('</div>');
      btParts.push('<button class="btn btn-ghost btn-sm" id="ds-bt-toggle">' +
        (bt.signals || []).length + ' 个信号明细</button>');
      parts.push('<div class="ds-bt">' + btParts.join('') + '</div>');
    } else if (bt && bt.error) {
      parts.push('<div class="ds-sub ds-issues">盘口回测：' + U.escapeHtml(bt.error) + '</div>');
    }

    const issues = result.issues || [];
    if (issues.length) {
      parts.push('<div class="ds-sub ds-issues">⚠ 发现 ' + issues.length + ' 个问题：<br>' +
        issues.map(function (i) { return '· ' + U.escapeHtml(i); }).join('<br>'));
    } else {
      parts.push('<div class="ds-sub">✅ 未发现问题：各数据源可用，数据均为最新。</div>');
    }
    parts.push('<div class="ds-actions">' +
      '<button class="btn btn-sm" id="ds-recheck">↻ 重新自检</button>' +
      '<button class="btn btn-ghost btn-sm" id="ds-back">返回</button></div>');
    pop.innerHTML = parts.join('');
    bindDsActions();
  }

  function renderDsMetaView() {
    const pop = document.getElementById('ds-popover');
    if (!pop || !state.meta) return;
    const providers = state.meta.providers || [];
    const throttled = state.meta.throttled_hosts || {};
    const ai = state.meta.ai || {};
    const parts = [];

    parts.push('<div class="ds-head">📡 数据源状态</div>');
    if (!providers.length) {
      parts.push('<div class="ds-row"><span class="ds-name err">无可用源</span>' +
        '<span class="ds-caps">请检查 PROVIDER_ORDER 配置</span></div>');
    } else {
      providers.forEach(function (p) {
        const h = providerHealth(p);
        const caps = (p.caps || []).map(function (c) { return CAP_LABELS[c] || c; }).join('·');
        let stateText = '正常';
        let cls = 'ok';
        if (p.cooling > 0) { stateText = '熔断冷却 ' + p.cooling + 's'; cls = 'err'; }
        else if (p.fails > 0) { stateText = '连续失败 ' + p.fails; cls = 'warn'; }
        parts.push('<div class="ds-row">' +
          '<span class="ds-name ' + cls + '">' + U.escapeHtml(p.name) + '</span>' +
          '<span class="ds-caps">' + caps + '</span>' +
          '<span class="ds-state ' + cls + '">' + stateText + '</span></div>');
      });
    }

    const tkeys = Object.keys(throttled);
    if (tkeys.length) {
      parts.push('<div class="ds-sub">⚠ 限流中的主机（已自动切换备用源）：<br>' +
        tkeys.map(function (h) {
          const s = throttled[h];
          return U.escapeHtml(h) + (s > 0 ? '（剩余 ' + Math.ceil(s) + 's）' : '');
        }).join('<br>'));
    }
    parts.push('<div class="ds-sub">AI 引擎：' +
      (ai.enabled ? U.escapeHtml(ai.model || 'LLM') : '内置规则引擎（未配置 API Key）') +
      '<br>刷新周期：行情 ' + (state.meta.refresh ? state.meta.refresh.quote_ttl : '--') + 's / 历史 ' +
      (state.meta.refresh ? state.meta.refresh.history_ttl : '--') + 's</div>');
    parts.push('<div class="ds-actions"><button class="btn btn-sm" id="ds-check">🔍 立即自检</button></div>');
    pop.innerHTML = parts.join('');
    bindDsActions();
  }

  async function runDsCheck() {
    if (dsState.checking) return;
    dsState.checking = true;
    dsState.view = 'check';
    renderDsCheckLoading();
    try {
      const result = await API.healthCheck();
      dsState.lastResult = result;
      renderDsCheckResult(result);
    } catch (err) {
      const pop = document.getElementById('ds-popover');
      pop.innerHTML = '<div class="ds-head">🔍 数据源自检</div>' +
        '<div class="ds-sub ds-issues">自检失败：' + U.escapeHtml(err.message || String(err)) + '</div>' +
        '<div class="ds-actions"><button class="btn btn-sm" id="ds-recheck">重试</button>' +
        '<button class="btn btn-ghost btn-sm" id="ds-back">返回</button></div>';
      bindDsActions();
    } finally {
      dsState.checking = false;
    }
  }

  function bindDsActions() {
    // 面板内按钮阻止冒泡：渲染会替换 popover 内容，事件冒泡到 document 的
    // 关闭处理器时旧节点已脱离文档，closest('.ds-wrap') 失效会误关面板
    const stop = function (fn) {
      return function (e) { e.stopPropagation(); fn(); };
    };
    const checkBtn = document.getElementById('ds-check');
    if (checkBtn) checkBtn.addEventListener('click', stop(runDsCheck));
    const recheckBtn = document.getElementById('ds-recheck');
    if (recheckBtn) recheckBtn.addEventListener('click', stop(runDsCheck));
    const btToggle = document.getElementById('ds-bt-toggle');
    if (btToggle) btToggle.addEventListener('click', stop(function () {
      const detail = document.querySelector('#ds-popover .ds-bt-detail');
      if (!detail) return;
      const open = detail.style.display !== 'block';
      detail.style.display = open ? 'block' : 'none';
      btToggle.textContent = open ? '收起信号明细' : (document.querySelectorAll('#ds-popover .ds-bt-row').length) + ' 个信号明细';
    }));
    const backBtn = document.getElementById('ds-back');
    if (backBtn) backBtn.addEventListener('click', stop(function () {
      dsState.view = 'meta';
      renderDsMetaView();
    }));
  }

  function renderDsPopover() {
    if (dsState.view === 'check') {
      if (dsState.lastResult) renderDsCheckResult(dsState.lastResult);
      else renderDsCheckLoading();
      return;
    }
    renderDsMetaView();
  }

  function toggleDsPopover(forceOpen) {
    const pop = document.getElementById('ds-popover');
    const btn = document.getElementById('btn-ds');
    if (!pop) return;
    const open = forceOpen != null ? forceOpen : pop.hidden;
    pop.hidden = !open;
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      dsState.view = 'meta';
      dsState.lastResult = null;
      renderDsPopover();
    }
  }

  function renderSubBar() {
    const bar = document.getElementById('topbar-sub');
    if (!bar || !state.meta) return;
    const ai = state.meta.ai || {};
    const parts = [];
    parts.push('AI 引擎：' + (ai.enabled ? ai.model : '内置规则引擎（未启用或未配置 API Key）'));
    const names = (state.meta.providers || []).map(function (p) { return p.name; });
    if (names.length) parts.push('数据源：' + names.join(' → '));
    const throttled = state.meta.throttled_hosts || {};
    const keys = Object.keys(throttled);
    if (keys.length) parts.push('⚠ 限流中：' + keys.join('、') + '（已自动切换备用源）');
    bar.textContent = parts.join('　·　');
  }

  function bind() {
    global.addEventListener('hashchange', route);
    document.getElementById('btn-refresh').addEventListener('click', manualRefresh);
    document.getElementById('btn-manage').addEventListener('click', function () {
      PageHome.toggleManage();
    });
    document.getElementById('btn-settings').addEventListener('click', function () {
      Settings.open();
    });
    document.getElementById('btn-ds').addEventListener('click', function (e) {
      e.stopPropagation();
      toggleDsPopover();
    });
    document.addEventListener('click', function (e) {
      const pop = document.getElementById('ds-popover');
      if (!pop.hidden && !e.target.closest('.ds-wrap')) toggleDsPopover(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !document.getElementById('ds-popover').hidden) toggleDsPopover(false);
    });
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) currentPage().tick();
    });
    // 每分钟同步一次交易时段与数据源健康度
    setInterval(loadMeta, 60000);
  }

  global.App = {
    setSession: setSession,
    getSession: function () { return state.session; },
    refreshMeta: loadMeta,
    start: async function () {
      bind();
      await loadMeta();
      await route();
    }
  };

  document.addEventListener('DOMContentLoaded', function () { App.start(); });
})(window);
