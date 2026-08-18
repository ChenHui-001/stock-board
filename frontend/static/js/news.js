/* 个股资讯弹窗：近一个月相关资讯，逐条附 AI 解读（LLM / 规则引擎） */
(function (global) {
  'use strict';

  const SENTIMENT_CLASS = {
    '利好': 'sent-bull',
    '利空': 'sent-bear',
    '中性': 'sent-flat'
  };

  const state = { code: null, name: '', loading: false };

  function root() { return document.getElementById('modal-root'); }
  function body() { return document.getElementById('modal-body'); }
  function titleNode() { return document.getElementById('modal-title'); }
  function actionsNode() { return document.getElementById('modal-actions'); }

  function show() {
    root().hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function close() {
    root().hidden = true;
    document.body.style.overflow = '';
    state.loading = false;
  }

  async function open(code, name, triggerBtn) {
    if (state.loading) return;
    state.code = code;
    state.name = name || code;

    titleNode().textContent = '股票资讯 · ' + state.name + ' (' + code + ')';
    actionsNode().innerHTML = '';
    renderLoading();
    show();

    if (triggerBtn) {
      triggerBtn.disabled = true;
      triggerBtn.classList.add('loading');
    }
    state.loading = true;

    try {
      const data = await API.news(code, false);
      renderList(data);
    } catch (err) {
      renderError(err.message);
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
    renderLoading();
    actionsNode().innerHTML = '';
    try {
      const data = await API.news(state.code, true);
      renderList(data);
    } catch (err) {
      renderError(err.message);
    } finally {
      state.loading = false;
    }
  }

  function renderLoading() {
    body().innerHTML =
      '<div class="ai-loading">'
      + '<div class="ai-spinner"></div>'
      + '<div class="ai-loading-text">正在获取近一个月相关资讯…</div>'
      + '<div class="ai-loading-sub">检索财经新闻并生成 AI 解读，请稍候</div>'
      + '</div>';
  }

  function renderError(msg) {
    body().innerHTML =
      '<div class="empty">'
      + '<div class="empty-icon">⚠️</div>'
      + '<div class="empty-title">资讯获取失败</div>'
      + '<div class="empty-desc">' + U.escapeHtml(msg) + '</div>'
      + '</div>';
    const retry = U.el('button', 'btn btn-sm btn-primary', '重试');
    retry.onclick = refresh;
    actionsNode().innerHTML = '';
    actionsNode().appendChild(retry);
  }

  function renderEmpty(meta) {
    body().innerHTML =
      '<div class="empty">'
      + '<div class="empty-icon">🗞️</div>'
      + '<div class="empty-title">近一个月暂无相关资讯</div>'
      + '<div class="empty-desc">' + U.escapeHtml((meta && meta.error) || '没有检索到该股票近 30 天的新闻') + '</div>'
      + '</div>';
    appendMeta(meta);
  }

  function appendMeta(meta) {
    if (!meta) return;
    const m = U.el('div', 'ai-meta');
    m.appendChild(U.el('span', '', '引擎：' + (meta.engine === 'llm' ? 'AI 大模型' : (meta.engine === 'rule' ? '内置规则引擎' : '--'))));
    m.appendChild(U.el('span', '', '获取时间：' + (meta.fetched_at || '--')));
    if (meta.total != null) m.appendChild(U.el('span', '', '共 ' + meta.total + ' 条'));
    body().appendChild(m);
  }

  function renderList(data) {
    const meta = data.meta || {};
    const items = data.items || [];
    const host = body();
    host.innerHTML = '';

    if (!items.length) {
      renderEmpty(meta);
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
      const title = U.el('a', 'news-title');
      title.textContent = item.title || '（无标题）';
      title.href = item.url || '#';
      title.target = '_blank';
      title.rel = 'noopener noreferrer';
      if (!item.url) title.style.pointerEvents = 'none';
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

    // 头部操作按钮
    const acts = actionsNode();
    acts.innerHTML = '';
    const againBtn = U.el('button', 'btn btn-sm btn-primary', '刷新');
    againBtn.onclick = refresh;
    acts.appendChild(againBtn);
  }

  // 关闭交互（与 AI 弹窗共用 modal-root 的 data-close / Esc 监听）
  document.addEventListener('click', function (e) {
    if (e.target && e.target.getAttribute && e.target.getAttribute('data-close') === '1') close();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !root().hidden) close();
  });

  global.News = { open: open, close: close };
})(window);
