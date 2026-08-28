/* 价值投资菜单页：候选池 + 估值标签 + 投资建议
   渲染 /api/value/screen 返回的 pools.core/trend/emotion 三个分层池。 */
(function (global) {
  'use strict';

  // ---- 池子语义说明（核心 / 趋势 / 情绪各看什么） ----
  const POOL_DESC = {
    core: '基本面合格 + 风险低，适合中长期价值投资;PE/PB 标签决定介入窗口。',
    trend: '综合分尚可，多在板块/资金/量价维度见长,关注突破或分歧低吸。',
    emotion: '连板梯队主导,情绪溢价高,游资博弈属性强,需结合题材热度。'
  };

  // 信号配色（与前端 tone 系统一致：up/down/warn/flat）
  const SIGNAL_TONE = {
    VALUE_BUY: 'up',
    QUALITY_HOLD: 'up',
    BUY: 'up',
    BREAKOUT_BUY: 'up',
    PULLBACK_BUY: 'up',
    WATCH: 'flat',
    REDUCE: 'warn',
    AVOID: 'down',
    EXIT: 'down'
  };

  // 估值带的颜色（深度低估=绿/低估=浅绿/合理=灰/偏高=橙/高估=红）
  function bandTone(band) {
    if (band === '深度低估' || band === '极低估') return 'up';
    if (band === '低估') return 'up-soft';
    if (band === '合理') return 'flat';
    if (band === '偏高' || band === '恶化') return 'warn';
    if (band === '高估') return 'down';
    if (band === '亏损') return 'down';
    if (band === '增速缺失') return 'flat';
    if (band === '偏弱') return 'warn';
    if (band === '健康' || band === '优秀') return 'up';
    return 'flat';
  }

  function peBandClass(band) { return 'val-band val-' + (bandTone(band) || 'flat'); }

  // ---- DOM helpers ----
  function bandTag(band) {
    if (!band) return U.el('span', 'val-band val-flat', U.NBSP);
    return U.el('span', peBandClass(band), band);
  }

  function signalTag(signal, advice) {
    const tone = SIGNAL_TONE[signal] || 'flat';
    const tag = U.el('span', 'val-signal val-' + tone, advice || signal || '—');
    if (signal) tag.title = '信号:' + signal;
    return tag;
  }

  // ---- 渲染单只股票 ----
  function row(s, navigate) {
    const tr = U.el('tr', 'val-row');
    tr.style.cursor = 'pointer';
    const fullCode = s.code + '.' + s.market;
    tr.onclick = function () { location.hash = '#/stock/' + encodeURIComponent(fullCode); };

    // 代码 / 名称
    const tdName = U.el('td', 'val-name');
    tdName.appendChild(U.el('div', 'val-code', s.code));
    tdName.appendChild(U.el('div', 'val-cname', s.name || ''));
    if (s.board) tdName.appendChild(U.el('div', 'val-board', s.board));
    tr.appendChild(tdName);

    // 价格 + 涨跌幅
    const tdPrice = U.el('td', 'val-price');
    tdPrice.appendChild(U.el('div', '', U.price(s.price)));
    if (s.change_pct != null) {
      const t = U.el('div', 'val-change ' + U.tone(s.change_pct), U.pct(s.change_pct));
      tdPrice.appendChild(t);
    }
    tr.appendChild(tdPrice);

    // PE + 标签
    const tdPe = U.el('td', 'val-pe');
    tdPe.appendChild(U.el('div', 'val-num', U.isNum(s.pe) ? s.pe.toFixed(1) : U.NBSP));
    tdPe.appendChild(bandTag(s.value_metrics && s.value_metrics.pe_band));
    tr.appendChild(tdPe);

    // PB + 标签
    const tdPb = U.el('td', 'val-pb');
    tdPb.appendChild(U.el('div', 'val-num', U.isNum(s.pb) ? s.pb.toFixed(2) : U.NBSP));
    tdPb.appendChild(bandTag(s.value_metrics && s.value_metrics.pb_band));
    tr.appendChild(tdPb);

    // PEG + 标签
    const tdPeg = U.el('td', 'val-peg');
    tdPeg.appendChild(bandTag(s.value_metrics && s.value_metrics.peg_band));
    if (s.value_metrics && s.value_metrics.industry_band) {
      const ib = U.el('div', 'val-meta', '行业强度 ' + s.value_metrics.industry_band);
      tdPeg.appendChild(ib);
    }
    tr.appendChild(tdPeg);

    // 综合分（带 grade）
    const tdScore = U.el('td', 'val-score');
    if (s.total_score != null) {
      tdScore.appendChild(U.el('div', 'val-num-big', s.total_score.toFixed(1)));
      const g = U.el('div', 'val-grade', (s.grade || '') + ' 级 · ' + (s.grade_name || ''));
      tdScore.appendChild(g);
    }
    tr.appendChild(tdScore);

    // 投资建议 = 信号的中文含义
    const tdSig = U.el('td', 'val-sig');
    tdSig.appendChild(signalTag(s.signal, s.advice));
    if (s.risk_notes && s.risk_notes.length) {
      const note = U.el('div', 'val-meta', '·' + s.risk_notes.slice(0, 2).join(' · '));
      tdSig.appendChild(note);
    }
    tr.appendChild(tdSig);

    return tr;
  }

  function renderPool(title, desc, list, navigate) {
    const wrap = U.el('div', 'val-pool');
    const head = U.el('div', 'val-pool-head');
    head.appendChild(U.el('div', 'val-pool-title', title));
    if (desc) head.appendChild(U.el('div', 'val-pool-desc', desc));
    wrap.appendChild(head);

    if (!list || !list.length) {
      wrap.appendChild(U.el('div', 'val-empty', '暂无数据'));
      return wrap;
    }

    const table = U.el('table', 'val-table');
    const thead = U.el('thead');
    const trh = U.el('tr');
    ['代码 · 名称', '现价 · 涨跌', 'PE · 估值', 'PB · 估值', 'PEG · 行业',
     '综合分 · 分级', '建议 · 风险点'].forEach(function (h) {
      trh.appendChild(U.el('th', '', h));
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = U.el('tbody');
    list.forEach(function (s) { tbody.appendChild(row(s, navigate)); });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function renderMarketBanner(mkt) {
    if (!mkt) return null;
    const ban = U.el('div', 'val-market');
    const state = (mkt.state || '?') + ' · ' + (mkt.name || '');
    const attack = U.isNum(mkt.attack) ? '进攻 ' + mkt.attack : '';
    const zt = U.isNum(mkt.zt_count) ? '涨停 ' + mkt.zt_count : '';
    const cand = U.isNum(mkt.candidate_count) ? '候选 ' + mkt.candidate_count : '';
    ban.appendChild(U.el('span', 'val-market-state', state));
    [attack, zt, cand].filter(Boolean).forEach(function (t) {
      ban.appendChild(U.el('span', 'val-market-meta', t));
    });
    return ban;
  }

  function renderBoards(boards) {
    if (!boards || !boards.length) return null;
    const wrap = U.el('div', 'val-boards');
    wrap.appendChild(U.el('div', 'val-pool-title', '最强板块'));
    const list = U.el('div', 'val-board-list');
    boards.slice(0, 10).forEach(function (b) {
      const chip = U.el('span', 'val-board-chip');
      chip.appendChild(U.el('span', '', b.name));
      chip.appendChild(U.el('span', 'val-board-strength', U.isNum(b.strength) ? b.strength.toFixed(2) : ''));
      list.appendChild(chip);
    });
    wrap.appendChild(list);
    return wrap;
  }

  // ---- Module API ----
  const state = {
    data: null,
    loading: false,
    refreshing: false,
    error: null
  };

  let viewEl = null;

  function skeleton() {
    viewEl.innerHTML = '';
    const root = U.el('div', 'page-value');
    const head = U.el('div', 'val-head');
    head.appendChild(U.el('h2', 'val-title', '💎 价值投资'));
    const right = U.el('div', 'val-head-right');
    const refresh = U.el('button', 'btn btn-sm btn-primary', '刷新数据');
    refresh.title = '强制重算候选池（API 调用较慢）';
    refresh.onclick = async function () {
      if (state.refreshing) return;
      state.refreshing = true;
      refresh.textContent = '刷新中…';
      refresh.disabled = true;
      try {
        state.data = await API.valueScreen(true);
        state.error = null;
        renderData(state.data);
      } catch (e) {
        state.error = e.message || String(e);
        viewEl.innerHTML = '';
        viewEl.appendChild(U.el('div', 'val-error', '加载失败: ' + state.error));
      } finally {
        state.refreshing = false;
        refresh.textContent = '刷新数据';
        refresh.disabled = false;
      }
    };
    right.appendChild(refresh);
    head.appendChild(right);
    root.appendChild(head);
    viewEl.appendChild(root);
  }

  function renderData(data) {
    skeleton();
    const root = viewEl.querySelector('.page-value');

    if (data.generated_at) {
      root.appendChild(U.el('div', 'val-gen',
        '生成于 ' + (data.generated_at || '') +
        (state.refreshing ? '' : ' · 缓存 15 分钟')));
    }

    const mktBan = renderMarketBanner(data.market);
    if (mktBan) root.appendChild(mktBan);

    const boards = renderBoards(data.board_top);
    if (boards) root.appendChild(boards);

    const pools = data.pools || {};
    Object.keys(pools).forEach(function (k) {
      const titleMap = { core: '核心机会池', trend: '趋势池', emotion: '情绪妖股池' };
      const poolEl = renderPool(titleMap[k] || k, POOL_DESC[k], pools[k] || []);
      root.appendChild(poolEl);
    });

    // 风险提示（市场本身的状态）
    if (data.market && data.market.tip) {
      root.appendChild(U.el('div', 'val-market-tip', data.market.tip));
    }
  }

  async function load() {
    if (!viewEl) return;
    skeleton();
    state.loading = true;
    try {
      state.data = await API.valueScreen(false);
      state.error = null;
      renderData(state.data);
    } catch (e) {
      state.error = e.message || String(e);
      viewEl.innerHTML = '';
      viewEl.appendChild(U.el('div', 'val-error', '加载失败: ' + state.error));
    } finally {
      state.loading = false;
    }
  }

  global.PageValue = {
    mount: function () {
      viewEl = document.getElementById('view');
      load();
    },
    refresh: async function () {
      // 显式刷新按钮触发的强刷,在 mount/refresh 之外也能用
      if (!viewEl) return;
      state.data = await API.valueScreen(false);
      renderData(state.data);
    },
    tick: function () {
      // 价值选股有 15 分钟缓存,tick 只在错误时轻提示
      if (state.error) U.toast('价值投资数据加载异常: ' + state.error, 'warn');
    }
  };
})(window);
