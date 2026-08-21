/* 后端 API 封装 */
(function (global) {
  'use strict';

  // 请求超时（毫秒）：普通取数接口用这个默认值
  const REQUEST_TIMEOUT = 90000;
  // 含 LLM 调用的接口：后端单次 LLM 超时默认 120s（代码层保底 ≥90s），
  // 再加上取数时间，浏览器侧必须留出余量，否则后端还在正常出结果、前端已判定超时
  const LLM_TIMEOUT = 180000;

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
    healthCheck: function (withBacktest, backtestDays) {
      // 数据源逐源实测，耗时较长（10-30s），给足超时；
      // withBacktest=false 跳过盘口回测段（更快、省数据源配额）；
      // backtestDays 控制回测样本深度（30-250 交易日）
      const p = [];
      if (withBacktest === false) p.push('with_backtest=0');
      if (withBacktest !== false && backtestDays) p.push('backtest_days=' + backtestDays);
      return request('/api/health/check' + (p.length ? '?' + p.join('&') : ''), { timeout: 120000 });
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
    hotspot: function (minutes, refresh) {
      var q = [];
      if (minutes != null) q.push('minutes=' + minutes);
      if (refresh) q.push('refresh=1');
      return request('/api/hotspot' + (q.length ? '?' + q.join('&') : ''));
    },
    hotspotAnalyze: function (item, refresh) {
      // 单条快讯 AI 分析：利好/利空行业 + 关联度最高股票。
      // 标题/摘要截断到后端字段上限内，避免超长快讯摘要触发 422
      return request('/api/hotspot/analyze' + (refresh ? '?refresh=1' : ''), {
        method: 'POST',
        timeout: LLM_TIMEOUT,
        body: {
          title: String((item && item.title) || '').slice(0, 500),
          summary: String((item && item.summary) || '').slice(0, 2000),
          source: String((item && (item.source || item.origin)) || '').slice(0, 100)
        }
      });
    },
    detail: function (code, refresh) {
      return request('/api/stock/' + encodeURIComponent(code) + (refresh ? '?refresh=1' : ''));
    },
    quote: function (code, refresh) {
      return request('/api/quote/' + encodeURIComponent(code) + (refresh ? '?refresh=1' : ''));
    },
    aiAnalyze: function (code, refresh) {
      // AI 分析是全站最慢的接口：后端串行「资讯解读 → 研报解读（这两步已并发）
      // → 主分析」，每段各自吃满 LLM_TIMEOUT（默认 120s，代码层保底 ≥90s）。
      // 浏览器侧上限必须高于后端最坏耗时（2×120s=240s），否则后端仍在正常
      // 工作、前端已报「请求超时」，用户看到失败而结果其实已经算完并写进当日缓存。
      return request('/api/ai/' + encodeURIComponent(code) + (refresh ? '?refresh=1' : ''), {
        method: 'POST',
        timeout: 300000
      });
    },
    news: function (code, refresh, days) {
      var q = [];
      if (refresh) q.push('refresh=1');
      if (days != null) q.push('days=' + days);
      return request('/api/news/' + encodeURIComponent(code) + (q.length ? '?' + q.join('&') : ''), { timeout: LLM_TIMEOUT });
    },
    reports: function (code, refresh, days) {
      var q = [];
      if (refresh) q.push('refresh=1');
      if (days != null) q.push('days=' + days);
      return request('/api/reports/' + encodeURIComponent(code) + (q.length ? '?' + q.join('&') : ''), { timeout: LLM_TIMEOUT });
    },
    scoreWeights: function () {
      return request('/api/score/weights');
    },
    valueScreen: function (refresh) {
      return request('/api/value/screen' + (refresh ? '?refresh=1' : ''), { timeout: 240000 });
    },
    scoreWeightsSave: function (w) {
      return request('/api/score/weights', { method: 'POST', body: w });
    },
    scoreWeightsReset: function () {
      return request('/api/score/weights/reset', { method: 'POST' });
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
