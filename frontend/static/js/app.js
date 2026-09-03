/* app（从 IIFE+global 转 ESM） */
import { U } from './util.js';
import { API } from './api.js';
import { Charts } from './charts.js';
import { AI } from './ai.js';
import { PageSearch } from './page-search.js';
import { PageValue } from './page-value.js';
import { PageHotspot } from './page-hotspot.js';
import { PageHome } from './page-home.js';
import { PageDetail } from './page-detail.js';
import { PageBacktest } from './page-backtest.js';
import { Settings } from './settings.js';


  const state = {
    route: null, param: null,
    // 上一个非 stock 路由,用于详情页「← 返回」导航到来源页面。
    // 只有在进入 stock 时才会更新;fallback 是 home。
    fromRoute: null, fromParam: null,
    timer: null, session: null, meta: null,
    // 当前挂载的页面模块。路由切换前用来调用它的 destroy() 卸载钩子
    mounted: null
  };

  function parseHash() {
    const hash = (location.hash || '#/home').replace(/^#\/?/, '');
    const parts = hash.split('/').filter(Boolean);
    if (!parts.length) return { route: 'home', param: null };
    if (parts[0] === 'stock' && parts[1]) return { route: 'stock', param: parts[1] };
    if (parts[0] === 'search') return { route: 'search', param: null };
    if (parts[0] === 'hotspot') return { route: 'hotspot', param: null };
    if (parts[0] === 'value') return { route: 'value', param: null };
    if (parts[0] === 'backtest') return { route: 'backtest', param: null };
    return { route: 'home', param: null };
  }

  function currentPage() {
    if (state.route === 'search') return PageSearch;
    if (state.route === 'stock') return PageDetail;
    if (state.route === 'hotspot') return PageHotspot;
    if (state.route === 'value') return PageValue;
    if (state.route === 'backtest') return PageBacktest;
    return PageHome;
  }

  function setNavActive() {
    document.querySelectorAll('.nav-item').forEach(function (node) {
      const r = node.getAttribute('data-route');
      const on = (r === 'home' && (state.route === 'home' || state.route === 'stock'))
        || (r === state.route);
      node.classList.toggle('active', on);
    });
    // 额外高亮来源 nav:从 value/hotspot 进入详情页时,顶部对应入口也高亮,
    // 让用户一眼看到当前详情页的来源页面。
    const sourceRoute = state.route === 'stock' ? state.fromRoute : state.route;
    if (sourceRoute && sourceRoute !== state.route) {
      document.querySelectorAll('.nav-item').forEach(function (node) {
        if (node.getAttribute('data-route') === sourceRoute) {
          node.classList.add('active');
        }
      });
    }
  }

  async function route() {
    const next = parseHash();
    // 卸载上一个页面：#view 是单容器，每次路由切换整块重建，页面里跨路由存活的
    // 状态（定时器/轮询/观察器）必须在这里停掉，否则会一直在后台跑，还会往已被
    // 替换掉的 DOM 里写内容。通用机制：页面模块只要导出 destroy，离开时就被调用。
    if (state.mounted && typeof state.mounted.destroy === 'function') {
      state.mounted.destroy();
    }
    state.mounted = null;
    // 记录「上一页」用于详情页返回:仅在进入 stock 时记录(从非 stock 跳入),
    // 这样 home→详情→返回→home→详情→返回 时 fromRoute 始终是 home;
    // 而 value→详情→返回 也能正确回到 value。
    // 已被记录后从 stock 跳 stock 不更新 fromRoute 也满足用户预期。
    if (next.route === 'stock' && state.route !== 'stock') {
      state.fromRoute = state.route;
      state.fromParam = state.param;
    }
    state.route = next.route;
    state.param = next.param;
    setNavActive();
    Charts.disposeAll();

    const manageBtn = document.getElementById('btn-manage');
    manageBtn.style.display = state.route === 'home' ? '' : 'none';

    state.mounted = currentPage();
    if (state.route === 'search') {
      PageSearch.mount();
    } else if (state.route === 'stock') {
      await PageDetail.mount(state.param);
    } else if (state.route === 'hotspot') {
      PageHotspot.mount();
    } else if (state.route === 'value') {
      PageValue.mount();
    } else if (state.route === 'backtest') {
      PageBacktest.mount();
    } else {
      await PageHome.mount();
    }
    restartTimer();
    // 页面切换淡入（内容渲染完成后重触发动画）
    const view = document.getElementById('view');
    view.classList.remove('page-enter');
    void view.offsetWidth;
    view.classList.add('page-enter');
  }

  // 详情页「← 返回」使用:跳回 fromRoute(通常是 home / value / hotspot / search),
  // 若 fromRoute 缺失或为 stock(理论上不应发生),fallback 到 home。
  function goBack() {
    const r = (state.fromRoute && state.fromRoute !== 'stock') ? state.fromRoute : 'home';
    const p = state.fromParam;
    const hash = (r === 'stock') ? '#/home' : (p ? '#/' + r + '/' + p : '#/' + r);
    location.hash = hash;
  }

  // 给详情页返回按钮用的标签:从哪儿来回哪儿去,无来源时显示「首页」。
  const ROUTE_BACK_LABEL = {
    home: '首页', value: '价值投资', hotspot: '热点追踪', search: '查询',
    backtest: '策略回测',
  };
  function backLabel() {
    const r = (state.fromRoute && state.fromRoute !== 'stock') ? state.fromRoute : 'home';
    return '← 返回' + (ROUTE_BACK_LABEL[r] || '首页');
  }

  function setSession(session) {
    if (!session) return;
    const changed = !state.session || state.session.trading !== session.trading;
    state.session = session;
    const badge = document.getElementById('session-badge');
    badge.innerHTML = session.label + (session.trading
      ? '<i class="session-hint"> · 5秒自动刷新</i>'
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
    // 返回 'ok' / 'warn' / 'err'：优先用综合评分，兼容旧字段
    if (p.cooling > 0) return 'err';
    if (typeof p.score === 'number') {
      if (p.score >= 0.7) return 'ok';
      if (p.score >= 0.4) return 'warn';
      return 'err';
    }
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
  const dsState = { view: 'meta', checking: false, lastResult: null, backtestDays: 120 };

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
      const flag = res.stale_flag ? ' (延迟)' : '';
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
    pop.innerHTML = '<div class="ds-head">' + U.iconHtml('search', { size: 14 }) + ' 数据源自检</div>' +
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
    parts.push('<div class="ds-head">' + U.iconHtml('search', { size: 14 }) + ' 数据源自检 <span class="ds-faint">' +
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

    // ---- 数据源限流降级提示（与回测降级提示同风格：ds-bt-note.warn）
    // 自检实测：某源任一能力报「限流冷却/频控」即视为限流；再叠加实时熔断状态兜底
    const thSrcs = (result.providers || []).filter(function (p) {
      return Object.keys(p.results || {}).some(function (c) {
        const e = (p.results[c] || {}).error || '';
        return e.indexOf('限流') >= 0 || e.indexOf('频控') >= 0;
      });
    }).map(function (p) { return p.name; });
    const thLive = Object.keys(state.meta.throttled_hosts || {})
      .filter(function (h) { return thSrcs.indexOf(h) < 0; });
    if (thSrcs.length || thLive.length) {
      const tlist = [];
      if (thSrcs.length) tlist.push(U.escapeHtml(thSrcs.join('、')) + ' 已触发频控');
      if (thLive.length) {
        tlist.push(thLive.map(function (h) {
          const s = (state.meta.throttled_hosts || {})[h];
          return U.escapeHtml(h) + (s > 0 ? '（冷却剩余 ' + Math.ceil(s) + 's）' : '');
        }).join('、') + ' 冷却中');
      }
      parts.push('<div class="ds-sub ds-bt-note warn">' + U.iconHtml('alert', { size: 14 }) + ' 数据源限流降级：' + tlist.join('；') +
        '，行情已自动切换备用源，其余功能不受影响</div>');
    }

    // ---- 盘口信号近期命中率（与回测脚本同口径）
    const bt = result.backtest;
    if (bt && bt.ok) {
      const btParts = [];
      const btConf = bt.confidence || {};
      const confBadge = function (c) {
        if (!c || !c.level) return '';
        return '<span class="ds-conf ds-conf-' + c.level + '" title="' + U.escapeHtml(c.note || '') + '">' +
          U.escapeHtml(c.label || '') + '</span>';
      };
      btParts.push('<div class="ds-bt-head">盘口信号近期命中率 <span class="ds-faint">' +
        bt.samples + ' 样本 / ' + bt.stocks + ' 只 · 基线 ' + bt.base_up_rate + '%</span>' +
        ' <span class="ds-bt-conf">置信度 ' + confBadge(btConf) + '</span></div>');
      // 分桶：vs基线>0 绿色、<0 红色；低置信度分桶淡化描边
      const buckRow = bt.buckets || [];
      if (buckRow.length) {
        btParts.push('<div class="ds-bt-buckets">' + buckRow.map(function (b) {
          const cls = b.vs_base >= 2 ? 'up' : (b.vs_base <= -2 ? 'down' : 'flat');
          const bcl = b.confidence && b.confidence.level === 'low' ? ' weak' : '';
          return '<span class="ds-bt-bucket ' + cls + bcl + '" title="样本 ' + b.n + ' · 平均涨跌 ' + b.avg_ret +
            '% · 置信度 ' + (b.confidence ? b.confidence.label : '-') + '">' +
            U.escapeHtml(b.bucket) + ' ' + b.up_rate + '%' + confBadge(b.confidence) + '</span>';
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
        const scl = s.confidence && s.confidence.level === 'low' ? ' weak' : '';
        return '<div class="ds-bt-row' + scl + '"><span>' + U.escapeHtml(s.signal) +
          (s.bullish ? ' 看多' : ' 看空') + '</span><span>n=' + s.n + '</span>' +
          '<span class="' + (s.hit_rate >= bt.base_up_rate + 2 ? 'up' : s.hit_rate <= bt.base_up_rate - 2 ? 'down' : '') + '">命中 ' + s.hit_rate + '%</span>' +
          confBadge(s.confidence) +
          '<span class="ds-faint">' + U.escapeHtml(s.advice) + '</span></div>';
      }).join(''));
      btParts.push('</div>');
      btParts.push('<button class="btn btn-ghost btn-sm" id="ds-bt-toggle">' +
        (bt.signals || []).length + ' 个信号明细</button>');
      parts.push('<div class="ds-bt">' + btParts.join('') + '</div>');
    } else if (bt && bt.skipped) {
      // 仅数据源自检（跳过回测）——灰字提示 + 天数选择 + 补跑按钮
      const days = [30, 90, 120, 250];
      parts.push('<div class="ds-sub ds-bt-note"><span class="ds-faint">盘口回测已跳过（仅数据源自检）</span>' +
        '<br>回测样本深度：' + days.map(function (d) {
          const cls = dsState.backtestDays === d ? 'active' : '';
          return '<button class="ds-day ' + cls + '" data-days="' + d + '">' +
            (d === 30 ? '近1月' : d === 90 ? '近3月' : d === 120 ? '近半年' : '近1年') + '</button>';
        }).join('') +
        '<div class="ds-actions"><button class="btn btn-sm" id="ds-bt-run">' + U.iconHtml('chartBar', { size: 14 }) + ' 补跑回测</button></div></div>');
    } else if (bt && bt.error) {
      // degraded：回测脚本未打包进镜像等环境问题——明确提示降级原因，其余自检正常
      if (bt.degraded) {
        parts.push('<div class="ds-sub ds-bt-note warn">' + U.iconHtml('alert', { size: 14 }) + ' 盘口回测已降级：' +
          U.escapeHtml(bt.error) +
          '<br><span class="ds-faint">其余数据源探测不受影响，如需回测功能请在镜像中打包 backtest_intraday.py</span></div>');
      } else {
        parts.push('<div class="ds-sub ds-issues">盘口回测：' + U.escapeHtml(bt.error) + '</div>');
      }
    }

    const issues = result.issues || [];
    if (issues.length) {
      parts.push('<div class="ds-sub ds-issues">' + U.iconHtml('alert', { size: 14 }) + ' 发现 ' + issues.length + ' 个问题：<br>' +
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

    parts.push('<div class="ds-head">' + U.iconHtml('bolt', { size: 14 }) + ' 数据源状态</div>');
    if (!providers.length) {
      parts.push('<div class="ds-row"><span class="ds-name err">无可用源</span>' +
        '<span class="ds-caps">请检查 PROVIDER_ORDER 配置</span></div>');
    } else {
      providers.forEach(function (p) {
        const h = providerHealth(p);
        const caps = (p.caps || []).map(function (c) { return CAP_LABELS[c] || c; }).join('·');
        const hasMetrics = typeof p.score === 'number';
        const scoreText = hasMetrics ? '评分 ' + Math.round(p.score * 100) : '';
        const latencyText = p.avg_latency_ms ? p.avg_latency_ms + 'ms' : '';
        const rateText = typeof p.success_rate === 'number' ? Math.round(p.success_rate * 100) + '%' : '';
        let stateText = '正常';
        let cls = 'ok';
        if (p.cooling > 0) { stateText = '熔断冷却 ' + p.cooling + 's'; cls = 'err'; }
        else if (hasMetrics && p.score < 0.4) { stateText = '质量差'; cls = 'err'; }
        else if (p.fails > 0) { stateText = '连续失败 ' + p.fails; cls = 'warn'; }
        const metrics = [scoreText, latencyText, rateText].filter(Boolean).join(' · ');
        const title = 'ok=' + (p.ok || 0) + ' / fail=' + (p.fail || 0) +
          (p.last_quote_time ? ' · 最新行情 ' + p.last_quote_time : '');
        parts.push('<div class="ds-row">' +
          '<span class="ds-name ' + cls + '" title="' + U.escapeHtml(title) + '">' + U.escapeHtml(p.name) + '</span>' +
          '<span class="ds-caps">' + caps + '</span>' +
          '<span class="ds-metrics">' + U.escapeHtml(metrics) + '</span>' +
          '<span class="ds-state ' + cls + '">' + stateText + '</span></div>');
      });
    }

    // 主机级自适应限流统计
    const hostStats = state.meta.host_stats || {};
    const hostKeys = Object.keys(hostStats);
    if (hostKeys.length) {
      parts.push('<div class="ds-sub">' + U.iconHtml('clock', { size: 14 }) + ' 主机自适应限流：<br>' +
        hostKeys.map(function (h) {
          const s = hostStats[h];
          const cooling = s.cooling > 0 ? ' · 冷却 ' + Math.ceil(s.cooling) + 's' : '';
          return U.escapeHtml(h) + '：间隔 ' + s.interval_actual + 's（基准 ' + s.interval_base +
            's × ' + s.multiplier + '）' + (s.ok_rate != null ? ' · 成功率 ' + Math.round(s.ok_rate * 100) + '%' : '') + cooling;
        }).join('<br>'));
    }

    const tkeys = Object.keys(throttled);
    if (tkeys.length) {
      parts.push('<div class="ds-sub">' + U.iconHtml('alert', { size: 14 }) + ' 限流中的主机（已自动切换备用源）：<br>' +
        tkeys.map(function (h) {
          const s = throttled[h];
          return U.escapeHtml(h) + (s > 0 ? '（剩余 ' + Math.ceil(s) + 's）' : '');
        }).join('<br>'));
    }
    parts.push('<div class="ds-sub">AI 引擎：' +
      (ai.enabled ? U.escapeHtml(ai.model || 'LLM') : '内置规则引擎（未配置 API Key）') +
      '<br>刷新周期：行情 ' + (state.meta.refresh ? state.meta.refresh.quote_ttl : '--') + 's / 历史 ' +
      (state.meta.refresh ? state.meta.refresh.history_ttl : '--') + 's</div>');
    parts.push('<div class="ds-actions">' +
      '<button class="btn btn-sm" id="ds-check">' + U.iconHtml('search', { size: 14 }) + ' 立即自检</button>' +
      '<button class="btn btn-ghost btn-sm" id="ds-check-fast" title="跳过盘口回测，仅数据源健康检查（更快）">' + U.iconHtml('bolt', { size: 14 }) + ' 仅数据源</button>' +
      '</div>');
    pop.innerHTML = parts.join('');
    bindDsActions();
  }

  async function runDsCheck(withBacktest) {
    if (dsState.checking) return;
    dsState.checking = true;
    dsState.view = 'check';
    dsState.withBacktest = withBacktest !== false;
    renderDsCheckLoading();
    try {
      const result = await API.healthCheck(dsState.withBacktest, dsState.backtestDays || 120);
      dsState.lastResult = result;
      renderDsCheckResult(result);
    } catch (err) {
      const pop = document.getElementById('ds-popover');
      pop.innerHTML = '<div class="ds-head">' + U.iconHtml('search', { size: 14 }) + ' 数据源自检</div>' +
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
    if (checkBtn) checkBtn.addEventListener('click', stop(function () { runDsCheck(true); }));
    const checkFast = document.getElementById('ds-check-fast');
    if (checkFast) checkFast.addEventListener('click', stop(function () { runDsCheck(false); }));
    const recheckBtn = document.getElementById('ds-recheck');
    if (recheckBtn) recheckBtn.addEventListener('click', stop(function () { runDsCheck(dsState.withBacktest !== false); }));
    const btToggle = document.getElementById('ds-bt-toggle');
    if (btToggle) btToggle.addEventListener('click', stop(function () {
      const detail = document.querySelector('#ds-popover .ds-bt-detail');
      if (!detail) return;
      const open = detail.style.display !== 'block';
      detail.style.display = open ? 'block' : 'none';
      btToggle.textContent = open ? '收起信号明细' : (document.querySelectorAll('#ds-popover .ds-bt-row').length) + ' 个信号明细';
    }));
    const btRun = document.getElementById('ds-bt-run');
    if (btRun) btRun.addEventListener('click', stop(function () { runDsCheck(true); }));
    // 回测样本深度选择（补跑回测区块内）
    document.querySelectorAll('#ds-popover .ds-day').forEach(function (b) {
      b.addEventListener('click', stop(function () {
        dsState.backtestDays = parseInt(b.getAttribute('data-days'), 10) || 120;
        document.querySelectorAll('#ds-popover .ds-day').forEach(function (x) {
          x.classList.toggle('active', x === b);
        });
      }));
    });
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
    if (keys.length) parts.push('限流中：' + keys.join('、') + '（已自动切换备用源）');
    bar.textContent = parts.join('　·　');
  }

  function bind() {
    window.addEventListener('hashchange', route);
    document.getElementById('btn-refresh').addEventListener('click', manualRefresh);
    document.getElementById('btn-manage').addEventListener('click', function () {
      PageHome.toggleManage();
    });
    // 显式绑定 nav 点击:既依赖浏览器跳转 href,又在同 hash 点击时强制刷新。
    // 解决两个常见「点击无反应」:
    //   (a) 用户在某个路由重复点同 nav,浏览器不会触发 hashchange → handler 兜底刷新
    //   (b) 某些浏览器扩展 / PWA 拦截 hash 跳转 → click handler 显式 setLocation
    document.querySelectorAll('.nav-item').forEach(function (node) {
      node.addEventListener('click', function (e) {
        // 注意:不要用 const route 命名,会遮蔽外层 route 函数。
        const targetRoute = node.getAttribute('data-route');
        if (!targetRoute) return;
        const target = (targetRoute === 'home') ? '#/home' : '#/' + targetRoute;
        // 同路由重复点击 / hash 未变:显式调用外层 route() 强制重渲。
        if (state.route === targetRoute) {
          e.preventDefault();
          route();
        } else if (location.hash === target) {
          // hash 已匹配但浏览器没触发 hashchange(罕见):直接调一次
          e.preventDefault();
          route();
        }
        // 否则让浏览器按 href 自然跳转,hashchange 会触发外层 route()。
      });
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

  export const App = {
    setSession: setSession,
    getSession: function () { return state.session; },
    refreshMeta: loadMeta,
    // 详情页「← 返回」导航到来源页面(从哪儿来回哪儿去)。
    goBack: goBack,
    backLabel: backLabel,
    // 一般不需要——从 stock 跳 stock 不更新 fromRoute 是预期行为。
    resetFromRoute: function () { state.fromRoute = null; state.fromParam = null; },
    start: async function () {
      bind();
      await loadMeta();
      await route();
    }
  };

// 启动时序说明（本模块与 page-home/page-detail/settings 存在循环依赖）：
// ESM 求值顺序是「先递归求值全部 import，再跑本模块体」，所以页面模块先于本段
// 代码执行——它们此刻引用 App 会触发 TDZ ReferenceError。当前契约：页面模块
// 【顶层禁止引用 App，只允许在函数体内使用】（函数体都在启动后才被调用）。
// module script 天然 defer（DOM 已就绪）→ 同步启动，bind() 的监听在 import返回
// 前注册完成（frontend_smoke.mjs 依赖这一点）；只有将来有人改成非 defer 加载
// （readyState=loading）才走 DOMContentLoaded 兜底，避免踩空 DOM。
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', function () { App.start(); });
} else {
  App.start();
}
