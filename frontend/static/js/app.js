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
    } catch (err) {
      document.getElementById('session-badge').textContent = '后端未就绪';
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
