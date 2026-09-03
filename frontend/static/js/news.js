/* news（从 IIFE+global 转 ESM） */
import { U } from './util.js';
import { API } from './api.js';
import * as M from './modal.js';
import { AI } from './ai.js';


  const SENTIMENT_CLASS = {
    '利好': 'sent-bull',
    '利空': 'sent-bear',
    '中性': 'sent-flat'
  };

  const RATING_CLASS = {
    '买入': 'rate-buy',
    '增持': 'rate-over',
    '中性': 'rate-flat',
    '减持': 'rate-bear',
    '卖出': 'rate-sell'
  };

  const state = { code: null, name: '', loading: false, tab: 'news', days: 365, newsDays: 30 };
  M.onClose(function () { state.loading = false; });

  // 时间范围筛选：资讯（近7天/近30天）+ 研报（近1月/近3月/近1年/全部）
  const NEWS_RANGE_OPTS = [['近7天', 7], ['近30天', 30]];
  const REPORT_RANGE_OPTS = [
    ['近1月', 30], ['近3月', 90], ['近1年', 365], ['全部', 0]
  ];

  async function open(code, name, triggerBtn) {
    if (state.loading) return;
    state.code = code;
    state.name = name || code;
    state.tab = 'news';
    state.days = 365;
    state.newsDays = 30;

    M.titleNode().textContent = '股票资讯 · ' + state.name + ' (' + code + ')';
    M.actionsNode().innerHTML = '';
    renderLoading('news');
    M.show();

    if (triggerBtn) {
      triggerBtn.disabled = true;
      triggerBtn.classList.add('loading');
    }
    state.loading = true;

    try {
      const data = await API.news(code, false);
      renderNews(data);
    } catch (err) {
      renderError(err.message, 'news');
    } finally {
      state.loading = false;
      if (triggerBtn) {
        triggerBtn.disabled = false;
        triggerBtn.classList.remove('loading');
      }
    }
  }

  async function refresh() {
    if (state.loading) return;
    state.loading = true;
    renderLoading(state.tab);
    M.actionsNode().innerHTML = '';
    try {
      if (state.tab === 'reports') {
        const data = await API.reports(state.code, true, state.days);
        renderReports(data);
      } else {
        const data = await API.news(state.code, true, state.newsDays);
        renderNews(data);
      }
    } catch (err) {
      renderError(err.message, state.tab);
    } finally {
      state.loading = false;
    }
  }

  function renderLoading(tab) {
    M.body().innerHTML =
      '<div class="ai-loading">'
      + '<div class="ai-spinner"></div>'
      + '<div class="ai-loading-text">' + (tab === 'reports' ? '正在获取券商研报…' : '正在获取近一个月相关资讯…') + '</div>'
      + '<div class="ai-loading-sub">' + (tab === 'reports' ? '从同花顺个股页抓取研报数据，请稍候' : '检索财经新闻并生成 AI 解读，请稍候') + '</div>'
      + '</div>';
  }

  function renderError(msg, tab) {
    M.body().innerHTML =
      '<div class="empty">'
      + '<div class="empty-icon">' + U.iconHtml('alert', { size: 40 }) + '</div>'
      + '<div class="empty-title">' + (tab === 'reports' ? '研报获取失败' : '资讯获取失败') + '</div>'
      + '<div class="empty-desc">' + U.escapeHtml(msg) + '</div>'
      + '</div>';
    const retry = U.el('button', 'btn btn-sm btn-primary', '重试');
    retry.onclick = refresh;
    M.actionsNode().innerHTML = '';
    M.actionsNode().appendChild(retry);
  }

  // ---------------------------------------------------------- 页签
  function renderTabs() {
    const tabs = U.el('div', 'news-tabs');
    [['news', '资讯'], ['reports', '研报']].forEach(function (t) {
      const btn = U.el('button', 'news-tab' + (state.tab === t[0] ? ' active' : ''), t[1]);
      btn.onclick = function () {
        if (state.loading || state.tab === t[0]) return;
        state.tab = t[0];
        M.actionsNode().innerHTML = '';
        renderLoading(t[0]);
        loadTab(t[0]);
      };
      tabs.appendChild(btn);
    });
    return tabs;
  }

  async function loadTab(tab) {
    state.loading = true;
    try {
      const data = tab === 'reports'
        ? await API.reports(state.code, false, state.days)
        : await API.news(state.code, false, state.newsDays);
      if (tab === 'reports') renderReports(data); else renderNews(data);
    } catch (err) {
      renderError(err.message, tab);
    } finally {
      state.loading = false;
    }
  }

  // 时间范围筛选条：资讯（近7天/近30天）+ 研报（近1月/近3月/近1年/全部）
  function renderRangeFilter(tab) {
    const opts = tab === 'reports' ? REPORT_RANGE_OPTS : NEWS_RANGE_OPTS;
    const cur = tab === 'reports' ? state.days : state.newsDays;
    const wrap = U.el('div', 'report-range');
    wrap.appendChild(U.el('span', 'report-range-label', '时间范围'));
    opts.forEach(function (opt) {
      const btn = U.el('button', 'range-btn' + (cur === opt[1] ? ' active' : ''), opt[0]);
      btn.dataset.days = String(opt[1]);
      btn.onclick = function (e) {
        e.stopPropagation();
        if (state.loading || cur === opt[1]) return;
        if (tab === 'reports') state.days = opt[1]; else state.newsDays = opt[1];
        renderLoading(tab);
        loadTab(tab);
      };
      wrap.appendChild(btn);
    });
    return wrap;
  }

  // ---------------------------------------------------------- 资讯
  function renderNews(data) {
    const meta = data.meta || {};
    const items = data.items || [];
    const host = M.body();
    host.innerHTML = '';
    host.appendChild(renderTabs());
    host.appendChild(renderRangeFilter('news'));

    if (!items.length) {
      host.appendChild(emptyBox('暂无相关资讯',
        U.icon('news', { size: 40 }), (meta && meta.error) || '该时间范围内没有检索到相关新闻'));
      appendMeta(meta);
      appendActions('refresh');
      return;
    }

    items.forEach(function (item, idx) {
      const card = U.el('div', 'news-card');

      // 头部：时间 + 来源 + 序号
      const head = U.el('div', 'news-head');
      head.appendChild(U.el('span', 'news-date', (item.date || '').slice(0, 16)));
      if (item.source) head.appendChild(U.el('span', 'news-source', item.source));
      head.appendChild(U.el('span', 'news-index', String(idx + 1).padStart(2, '0')));
      card.appendChild(head);

      // 标题（外链）
      // 外链地址过白名单：第三方源可能给出 javascript:/data: 地址，
      // 非 http(s) 一律退化为不可点的纯文本标题
      const href = U.safeUrl(item.url);
      const title = U.el('a', 'news-title');
      title.textContent = item.title || '（无标题）';
      title.href = href || '#';
      title.target = '_blank';
      title.rel = 'noopener noreferrer';
      if (!href) title.style.pointerEvents = 'none';
      card.appendChild(title);

      // 摘要
      if (item.summary) {
        card.appendChild(U.el('div', 'news-summary', item.summary));
      }

      // AI 解读
      const itp = item.interpretation || {};
      const sentCls = SENTIMENT_CLASS[itp.sentiment] || 'sent-flat';
      const box = U.el('div', 'news-interp');
      const tags = U.el('div', 'news-interp-tags');
      tags.appendChild(U.el('span', 'sent-tag ' + sentCls, (itp.sentiment || '中性') + (itp.impact ? ' · ' + itp.impact + '影响' : '')));
      if (itp.engine === 'llm') {
        tags.appendChild(U.el('span', 'interp-engine', 'AI 解读' + (itp.model ? ' · ' + itp.model : '')));
      } else {
        tags.appendChild(U.el('span', 'interp-engine', '规则解读'));
      }
      box.appendChild(tags);
      if (itp.summary) box.appendChild(U.el('div', 'news-interp-text', itp.summary));
      card.appendChild(box);

      host.appendChild(card);
    });

    appendMeta(meta);
    if (meta.error) {
      const warn = U.el('div', 'notice');
      warn.style.marginTop = '10px';
      warn.textContent = meta.error;
      host.appendChild(warn);
    }
    appendActions('refresh');
  }

  // ---------------------------------------------------------- 研报
  const RATE_COLOR = {
    '买入': 'rgba(245,70,93,.85)',
    '增持': 'rgba(251,191,36,.85)',
    '中性': 'rgba(148,163,184,.75)',
    '减持': 'rgba(23,178,106,.8)',
    '卖出': 'rgba(71,85,105,.8)'
  };
  const RATE_ORDER = ['买入', '增持', '中性', '减持', '卖出'];

  function renderRatingDist(dist) {
    const entries = Object.entries(dist || {}).filter(function (e) { return e[1] > 0; });
    if (!entries.length) return null;
    const total = entries.reduce(function (s, e) { return s + e[1]; }, 0);
    const ordered = RATE_ORDER
      .map(function (r) { return [r, dist[r] || 0]; })
      .filter(function (e) { return e[1] > 0; });
    const others = entries.filter(function (e) { return RATE_ORDER.indexOf(e[0]) < 0; });
    const all = ordered.concat(others);

    const wrap = U.el('div', 'report-dist');
    const bar = U.el('div', 'report-dist-bar');
    all.forEach(function (e) {
      const seg = U.el('div', 'report-dist-seg');
      seg.style.width = (e[1] / total * 100).toFixed(1) + '%';
      seg.style.background = RATE_COLOR[e[0]] || 'rgba(148,163,184,.6)';
      seg.title = e[0] + ' ' + e[1] + ' 份';
      bar.appendChild(seg);
    });
    wrap.appendChild(bar);

    const legend = U.el('div', 'report-dist-legend');
    all.forEach(function (e) {
      const item = U.el('span', 'report-dist-item');
      const dot = U.el('i', '', '');
      dot.style.background = RATE_COLOR[e[0]] || 'rgba(148,163,184,.6)';
      item.appendChild(dot);
      item.appendChild(document.createTextNode(e[0] + ' ' + e[1] + ' · ' + Math.round(e[1] / total * 100) + '%'));
      legend.appendChild(item);
    });
    wrap.appendChild(legend);
    return wrap;
  }

  function renderReports(data) {
    const meta = data.meta || {};
    const items = data.items || [];
    const host = M.body();
    host.innerHTML = '';
    host.appendChild(renderTabs());
    host.appendChild(renderRangeFilter('reports'));

    // 评级分布统计条（当前时间范围）
    const distNode = renderRatingDist(data.rating_dist);
    if (distNode) host.appendChild(distNode);

    if (!items.length) {
      host.appendChild(emptyBox('暂无券商研报',
        U.icon('fileText', { size: 40 }), (meta && meta.error) || '该股暂无收录的券商研报'));
      appendMeta(meta);
      appendActions('refresh');
      return;
    }

    items.forEach(function (item, idx) {
      const card = U.el('div', 'news-card');

      const head = U.el('div', 'news-head');
      head.appendChild(U.el('span', 'news-date', (item.date || '').slice(0, 10)));
      if (item.source) head.appendChild(U.el('span', 'news-source', item.source));
      if (item.researcher) head.appendChild(U.el('span', 'interp-engine', item.researcher));
      head.appendChild(U.el('span', 'news-index', String(idx + 1).padStart(2, '0')));
      card.appendChild(head);

      // 外链地址过白名单：第三方源可能给出 javascript:/data: 地址，
      // 非 http(s) 一律退化为不可点的纯文本标题
      const href = U.safeUrl(item.url);
      const title = U.el('a', 'news-title');
      title.textContent = item.title || '（无标题）';
      title.href = href || '#';
      title.target = '_blank';
      title.rel = 'noopener noreferrer';
      if (!href) title.style.pointerEvents = 'none';
      card.appendChild(title);

      const box = U.el('div', 'news-interp');
      const tags = U.el('div', 'news-interp-tags');
      const rating = item.rating || '--';
      tags.appendChild(U.el('span', 'rate-tag ' + (RATING_CLASS[rating] || 'rate-flat'), '评级 ' + rating));
      // AI 解读（与资讯解读同款：LLM / 规则双路径）
      const itp = item.interpretation || {};
      const sentCls = SENTIMENT_CLASS[itp.sentiment] || 'sent-flat';
      tags.appendChild(U.el('span', 'sent-tag ' + sentCls, (itp.sentiment || '中性') + (itp.impact ? ' · ' + itp.impact + '影响' : '')));
      tags.appendChild(U.el('span', 'interp-engine', itp.engine === 'llm'
        ? 'AI 解读' + (itp.model ? ' · ' + itp.model : '')
        : '规则解读'));
      box.appendChild(tags);
      if (itp.summary) box.appendChild(U.el('div', 'news-interp-text', itp.summary));
      card.appendChild(box);

      host.appendChild(card);
    });

    appendMeta(meta);
    if (meta.error) {
      const warn = U.el('div', 'notice');
      warn.style.marginTop = '10px';
      warn.textContent = meta.error;
      host.appendChild(warn);
    }
    appendActions('refresh');
  }

  function emptyBox(title, desc, icon) {
    const box = U.el('div', 'empty');
    box.appendChild(U.el('div', 'empty-icon', icon));
    box.appendChild(U.el('div', 'empty-title', title));
    box.appendChild(U.el('div', 'empty-desc', U.escapeHtml(desc || '')));
    return box;
  }

  function appendActions(kind) {
    const acts = M.actionsNode();
    acts.innerHTML = '';
    if (kind === 'refresh') {
      const againBtn = U.el('button', 'btn btn-sm btn-primary', '刷新');
      againBtn.onclick = refresh;
      acts.appendChild(againBtn);
    }
  }

  const NEWS_SOURCE_NAME = {
    eastmoney: '东方财富',
    sina: '新浪财经',
    ths: '同花顺',
    '': ''
  };

  function appendMeta(meta) {
    if (!meta) return;
    const m = U.el('div', 'ai-meta');
    if (meta.source) {
      m.appendChild(U.el('span', '', '来源：' + (NEWS_SOURCE_NAME[meta.source] || meta.source)));
    }
    m.appendChild(U.el('span', '', '获取时间：' + (meta.fetched_at || '--')));
    if (meta.total != null) m.appendChild(U.el('span', '', '共 ' + meta.total + ' 条'));
    M.body().appendChild(m);
  }

  export const News = { open: open, close: M.close };
