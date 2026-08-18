/* 后端 API 封装 */
(function (global) {
  'use strict';

  // 请求超时（毫秒）：LLM 分析最坏 3 次重试，给足余量；超时提示而非无限转圈
  const REQUEST_TIMEOUT = 90000;

  function fetchWithTimeout(path, opts) {
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, opts.timeout || REQUEST_TIMEOUT);
    opts.signal = ctrl.signal;
    return fetch(path, opts).finally(function () { clearTimeout(timer); });
  }

  async function request(path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    if (opts.body && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
      opts.headers['Content-Type'] = 'application/json';
    }
    let resp;
    try {
      resp = await fetchWithTimeout(path, opts);
    } catch (e) {
      if (e && e.name === 'AbortError') {
        const err = new Error('请求超时，请稍后重试');
        err.status = 0;
        throw err;
      }
      throw e;
    }
    let data = null;
    const text = await resp.text();
    if (text) {
      try { data = JSON.parse(text); } catch (e) { data = { detail: text }; }
    }
    if (!resp.ok) {
      const msg = (data && (data.detail || data.message)) || ('请求失败 ' + resp.status);
      const err = new Error(msg);
      err.status = resp.status;
      throw err;
    }
    return data;
  }

  global.API = {
    meta: function () {
      return request('/api/meta');
    },
    healthCheck: function () {
      // 数据源逐源实测，耗时较长（10-30s），给足超时
      return request('/api/health/check', { timeout: 120000 });
    },
    watchlist: function (refresh) {
      return request('/api/watchlist' + (refresh ? '?refresh=1' : ''));
    },
    addWatch: function (code, name, board) {
      return request('/api/watchlist', {
        method: 'POST',
        body: { code: code, name: name || null, board: board || null }
      });
    },
    removeWatch: function (codes) {
      return request('/api/watchlist/remove', { method: 'POST', body: { codes: codes } });
    },
    reorder: function (codes) {
      return request('/api/watchlist/order', { method: 'POST', body: { codes: codes } });
    },
    search: function (keyword, limit) {
      return request('/api/search?q=' + encodeURIComponent(keyword) + '&limit=' + (limit || 15));
    },
    hot: function (limit) {
      return request('/api/hot?limit=' + (limit || 8));
    },
    detail: function (code, refresh) {
      return request('/api/stock/' + encodeURIComponent(code) + (refresh ? '?refresh=1' : ''));
    },
    quote: function (code, refresh) {
      return request('/api/quote/' + encodeURIComponent(code) + (refresh ? '?refresh=1' : ''));
    },
    aiAnalyze: function (code, refresh) {
      return request('/api/ai/' + encodeURIComponent(code) + (refresh ? '?refresh=1' : ''), {
        method: 'POST'
      });
    },
    news: function (code, refresh) {
      return request('/api/news/' + encodeURIComponent(code) + (refresh ? '?refresh=1' : ''));
    },
    reports: function (code, refresh) {
      return request('/api/reports/' + encodeURIComponent(code) + (refresh ? '?refresh=1' : ''));
    },
    llmConfig: function () {
      return request('/api/llm/config');
    },
    llmSave: function (cfg) {
      return request('/api/llm/config', { method: 'POST', body: cfg });
    },
    llmTest: function (cfg) {
      return request('/api/llm/test', { method: 'POST', body: cfg });
    },
    llmModels: function (cfg) {
      return request('/api/llm/models', { method: 'POST', body: cfg });
    },
    llmReset: function () {
      return request('/api/llm/reset', { method: 'POST' });
    }
  };
})(window);
