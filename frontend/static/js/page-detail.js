/* 股票详情页：基础行情 / 均线 / 30日两融 / 30日资金流向 / 当前状态（需求 5.x） */
(function (global) {
  'use strict';

  // seq：请求代号。每次 mount 自增，异步响应回来时若代号已变，说明用户已切到
  // 别的股票，这份响应必须丢弃——数据源被频控时详情要几秒才回，快速切换 A→B
  // 会让 A 的慢响应后到并覆盖 B，出现"路由是 B、数据是 A"的错位。
  // ticking：轮询在途标记，防止 5s 心跳快于响应时堆叠并发请求。
  const state = { code: null, data: null, seq: 0, ticking: false };

  const MA_DESC = {
    5: '短期趋势线',
    10: '短期强弱',
    20: '中期·月线',
    60: '中长期·季线'
  };

  function view() { return document.getElementById('view'); }

  async function mount(code) {
    state.code = code;
    state.data = null;
    state.seq += 1;
    Charts.disposeAll();
    view().innerHTML = '<div class="card"><div class="loading-block">加载 ' + U.escapeHtml(code) + ' 详情数据…</div></div>';
    await load(false);
  }

  async function load(force) {
    const seq = state.seq;
    try {
      const data = await API.detail(state.code, force);
      if (seq !== state.seq) return;      // 已切到别的股票，丢弃这份过期响应
      state.data = data;
      render();
      App.setSession(state.data.session);
    } catch (err) {
      if (seq !== state.seq) return;      // 过期请求的失败不应覆盖当前页面
      view().innerHTML = '<div class="card"><div class="empty">'
        + '<div class="empty-icon">⚠️</div>'
        + '<div class="empty-title">详情加载失败</div>'
        + '<div class="empty-desc">' + U.escapeHtml(err.message) + '</div>'
        + '</div></div>';
    }
  }

  function render() {
    const d = state.data;
    const root = view();
    Charts.disposeAll();
    root.innerHTML = '';

    root.appendChild(renderHead(d));

    if (d.quote.status && d.quote.status !== 'normal' && d.quote.status_text) {
      root.appendChild(notice(d.quote.status_text + '，页面数据可能非最新成交价。', ''));
    }
    if (d.sources && d.sources.stale && d.sources.stale.length) {
      root.appendChild(notice(
        d.sources.stale.join('、') + ' 数据源当前不可用，已展示最近一次成功获取的数据。', ''));
    }

    root.appendChild(section('基础行情', renderQuoteGrid(d), null));
    root.appendChild(section('当前股票状态', renderStatus(d), '整合均线、资金、两融数据输出的标准化状态标签'));
    root.appendChild(section('均线数据（MA5 / MA10 / MA20 / MA60）', renderMa(d), maSubtitle(d)));
    root.appendChild(section('30 天资金流向', renderFlow(d), flowSubtitle(d)));
    root.appendChild(section('30 天两融数据', renderMargin(d), marginSubtitle(d)));
    root.appendChild(section('财报数据（季报 / 中报 / 三季报 / 年报）', renderFinancials(d), financialsSubtitle(d)));
    root.appendChild(renderSourceFooter(d));

    // 图表要在 DOM 挂载后再初始化
    requestAnimationFrame(function () {
      Charts.maChart(document.getElementById('chart-ma'), d.kline, d.ma_summary.series);
      const flowRows = d.fund_flow.rows;
      if (flowRows && flowRows.length) {
        Charts.flowChart(document.getElementById('chart-flow'), flowRows, !!d.fund_flow.summary.tiered);
      }
      const marginRows = d.margin.rows;
      if (marginRows && marginRows.length) {
        Charts.marginChart(document.getElementById('chart-margin'), marginRows);
        Charts.marginFlowChart(document.getElementById('chart-margin-flow'), marginRows);
      }
    });
  }

  function notice(text, kind) {
    return U.el('div', 'notice' + (kind ? ' ' + kind : ''), text);
  }

  function section(title, body, subtitle) {
    const card = U.el('div', 'card section');
    const head = U.el('div', 'card-head');
    head.appendChild(U.el('div', 'card-title', title));
    if (subtitle) head.appendChild(U.el('div', 'card-sub', subtitle));
    card.appendChild(head);
    const inner = U.el('div', 'card-body');
    inner.appendChild(body);
    card.appendChild(inner);
    return card;
  }

  // ---------------------------------------------------------- 顶部
  function renderHead(d) {
    const q = d.quote;
    const wrap = U.el('div', 'detail-head');

    const nav = U.el('div', 'detail-nav');
    const back = U.el('button', 'btn btn-sm', '← 返回首页');
    back.onclick = function () { location.hash = '#/home'; };
    nav.appendChild(back);

    const aiBtn = U.el('button', 'btn btn-sm btn-primary', 'AI 分析');
    aiBtn.onclick = function () { AI.open(q.code, q.name, aiBtn); };
    nav.appendChild(aiBtn);

    const watchBtn = U.el('button', 'btn btn-sm', d.watched ? '已在自选' : '+ 加入自选');
    watchBtn.disabled = !!d.watched;
    watchBtn.onclick = async function () {
      watchBtn.disabled = true;
      try {
        await API.addWatch(q.code, q.name, q.board);
        watchBtn.textContent = '已在自选';
        U.toast('已加入自选', 'ok');
      } catch (err) {
        watchBtn.disabled = false;
        U.toast('添加失败：' + err.message, 'err');
      }
    };
    nav.appendChild(watchBtn);
    wrap.appendChild(nav);

    const titleRow = U.el('div', 'detail-title-row');
    titleRow.appendChild(U.el('div', 'detail-name', q.name || q.code));
    titleRow.appendChild(U.el('div', 'detail-code', q.code + '.' + q.market));

    const boards = U.el('div', 'board-tags');
    const list = (d.boards && d.boards.length) ? d.boards.slice(0, 6) : (q.board ? [q.board] : []);
    list.forEach(function (b) { boards.appendChild(U.el('span', 'tag', b)); });
    titleRow.appendChild(boards);
    wrap.appendChild(titleRow);

    const priceRow = U.el('div', 'detail-price-row');
    const tone = U.tone(q.change_pct);
    priceRow.appendChild(U.el('div', 'detail-price ' + tone, U.price(q.price)));
    const chg = U.el('div', 'detail-change ' + tone);
    chg.textContent = (U.isNum(q.change) ? (q.change > 0 ? '+' : '') + q.change.toFixed(2) : U.NBSP)
      + '  ' + U.pct(q.change_pct);
    priceRow.appendChild(chg);
    wrap.appendChild(priceRow);

    return wrap;
  }

  function renderQuoteGrid(d) {
    const q = d.quote;
    const grid = U.el('div', 'quote-grid');
    const cells = [
      ['昨收', U.price(q.prev_close), ''],
      ['今开', U.price(q.open), toneVs(q.open, q.prev_close)],
      ['最高', U.price(q.high), toneVs(q.high, q.prev_close)],
      ['最低', U.price(q.low), toneVs(q.low, q.prev_close)],
      ['成交量', U.volume(q.volume), ''],
      ['成交额', U.money(q.amount), ''],
      ['换手率', U.isNum(q.turnover) ? q.turnover.toFixed(2) + '%' : U.NBSP, ''],
      ['振幅', amplitude(q), '']
    ];
    cells.forEach(function (c) {
      const cell = U.el('div', 'quote-cell');
      cell.appendChild(U.el('div', 'quote-label', c[0]));
      cell.appendChild(U.el('div', 'quote-value ' + c[2], c[1]));
      grid.appendChild(cell);
    });
    return grid;
  }

  function toneVs(v, base) {
    if (!U.isNum(v) || !U.isNum(base) || base === 0) return '';
    return U.tone(v - base);
  }

  function amplitude(q) {
    if (!U.isNum(q.high) || !U.isNum(q.low) || !U.isNum(q.prev_close) || !q.prev_close) return U.NBSP;
    return ((q.high - q.low) / q.prev_close * 100).toFixed(2) + '%';
  }

  // ---------------------------------------------------------- 状态标签
  function renderStatus(d) {
    const wrap = U.el('div');
    const tags = U.el('div', 'status-tags');
    (d.status.tags || []).forEach(function (t) {
      const node = U.el('div', 'status-tag ' + (t.tone || 'flat'));
      node.appendChild(U.el('div', 'g', t.group));
      node.appendChild(U.el('div', 'v ' + (t.tone === 'up' ? 'up' : t.tone === 'down' ? 'down' : ''), t.label));
      tags.appendChild(node);
    });
    wrap.appendChild(tags);

    const sr = d.support_resistance || {};
    const stats = U.el('div', 'stat-row');
    stats.style.marginTop = '14px';
    stats.style.marginBottom = '0';
    // 区间位置按 50% 阈值染色：上半区偏多（绿） / 下半区偏空（红），
    // 给用户一眼的位置感；5/20/60 日涨跌与均线段重复，不重复染色，仅展示。
    const rangePosTone = U.isNum(sr.range_pos_pct)
      ? (sr.range_pos_pct >= 50 ? 'up' : 'down')
      : '';
    [
      ['当前支撑位', U.price(sr.support) + (sr.support_from ? ' (' + sr.support_from + ')' : '')],
      ['当前压力位', U.price(sr.resistance) + (sr.resistance_from ? ' (' + sr.resistance_from + ')' : '')],
      ['20日区间', U.price(sr.low_20) + ' ~ ' + U.price(sr.high_20)],
      ['60日区间', U.price(sr.low_60) + ' ~ ' + U.price(sr.high_60)],
      ['区间位置', U.isNum(sr.range_pos_pct) ? sr.range_pos_pct.toFixed(1) + '%' : U.NBSP, rangePosTone],
      ['近5日涨跌', U.pct(d.status.trend.chg_5d)],
      ['近20日涨跌', U.pct(d.status.trend.chg_20d)],
      ['近60日涨跌', U.pct(d.status.trend.chg_60d)]
    ].forEach(function (s, i) {
      const node = U.el('div', 'stat');
      node.appendChild(U.el('div', 'stat-label', s[0]));
      node.appendChild(U.el('div', 'stat-value' + (s[2] ? ' ' + s[2] : ''), s[1]));
      stats.appendChild(node);
    });
    wrap.appendChild(stats);
    return wrap;
  }

  // ---------------------------------------------------------- 均线
  function maSubtitle(d) {
    const s = d.ma_summary || {};
    const above = (s.above || []).map(function (w) { return 'MA' + w; });
    const below = (s.below || []).map(function (w) { return 'MA' + w; });
    const parts = [s.arrangement];
    if (above.length) parts.push('站上 ' + above.join('/'));
    if (below.length) parts.push('跌破 ' + below.join('/'));
    return parts.join(' · ');
  }

  function renderMa(d) {
    const wrap = U.el('div');
    const grid = U.el('div', 'ma-grid');

    (d.ma || []).forEach(function (m) {
      const card = U.el('div', 'ma-card');
      const head = U.el('div', 'ma-card-head');
      head.appendChild(U.el('div', 'ma-name', 'MA' + m.window));
      head.appendChild(U.el('div', 'ma-desc', MA_DESC[m.window] || ''));
      card.appendChild(head);

      card.appendChild(U.el('div', 'ma-value', U.isNum(m.value) ? m.value.toFixed(2) : U.NBSP));

      const meta = U.el('div', 'ma-meta');
      const posTone = m.position === '站上' ? 'up' : m.position === '跌破' ? 'down' : '';
      meta.appendChild(U.el('span', 'tag ' + posTone, '股价' + m.position));
      const slopeTone = m.slope === '上行' ? 'up' : m.slope === '下行' ? 'down' : '';
      meta.appendChild(U.el('span', 'tag ' + slopeTone, '均线' + m.slope));
      if (U.isNum(m.deviation_pct)) {
        meta.appendChild(U.el('span', 'tag ' + U.tone(m.deviation_pct),
          '乖离 ' + (m.deviation_pct > 0 ? '+' : '') + m.deviation_pct.toFixed(2) + '%'));
      }
      card.appendChild(meta);
      grid.appendChild(card);
    });
    wrap.appendChild(grid);

    const chart = U.el('div', 'chart');
    chart.id = 'chart-ma';
    chart.style.marginTop = '14px';
    wrap.appendChild(chart);

    return wrap;
  }

  // ---------------------------------------------------------- 资金流向
  function flowSubtitle(d) {
    const s = d.fund_flow.summary || {};
    if (!s.available) return '暂无资金流向数据';
    return s.days + ' 个交易日 · ' + s.trend + ' · ' + s.state;
  }

  // 当日资金流向未发布（盘中 / 收盘后 16 点前）：东财/新浪日级资金流向通常
  // 16 点后才有当日数据，此时最后一行是前一交易日，必须明确标注日期避免误导
  function flowFreshNotice(d) {
    const s = d.fund_flow.summary || {};
    if (!s || !s.available || s.fresh) return '';
    return '当日资金流向尚未发布（通常收盘后 16 点更新），以下展示最近交易日 '
      + (s.last_date || '--') + ' 数据；状态判定已退回近5日口径。';
  }

  function renderFlow(d) {
    const wrap = U.el('div');
    const s = d.fund_flow.summary || {};
    const rows = d.fund_flow.rows || [];

    if (!rows.length) {
      wrap.appendChild(U.el('div', 'loading-block',
        d.fund_flow.error ? ('资金流向暂不可用：' + d.fund_flow.error) : '暂无资金流向数据'));
      return wrap;
    }

    if (!s.tiered) {
      wrap.appendChild(notice(
        '当前资金数据来自备用源（' + (d.fund_flow.source || '未知') + '），'
        + '仅提供净流入与超大单口径，无大单/中单/小单四档拆分。', 'info'));
    }

    const freshNotice = flowFreshNotice(d);
    if (freshNotice) {
      wrap.appendChild(notice(freshNotice, 'warn'));
    }

    const stats = U.el('div', 'stat-row');
    const lastLabel = s.fresh ? '当日主力' : '最近交易日主力';
    const items = [
      ['30日主力净额', U.signedMoney(s.main_total), U.tone(s.main_total)],
      ['近5日主力', U.signedMoney(s.main_last5), U.tone(s.main_last5)],
      [lastLabel, U.signedMoney(s.main_last), U.tone(s.main_last)],
      ['超大单合计', U.signedMoney(s.xl_total), U.tone(s.xl_total)]
    ];
    if (s.tiered) {
      items.push(['大单合计', U.signedMoney(s.lg_total), U.tone(s.lg_total)]);
      items.push(['中单合计', U.signedMoney(s.md_total), U.tone(s.md_total)]);
      items.push(['小单(散户)', U.signedMoney(s.sm_total), U.tone(s.sm_total)]);
    }
    items.push(['流入/流出天数', s.inflow_days + ' / ' + s.outflow_days, '']);
    items.push(['当前连续', s.streak + ' 天' + s.streak_dir,
      s.streak_dir === '流入' ? 'up' : s.streak_dir === '流出' ? 'down' : '']);

    items.forEach(function (it) {
      const node = U.el('div', 'stat');
      node.appendChild(U.el('div', 'stat-label', it[0]));
      node.appendChild(U.el('div', 'stat-value ' + it[2], it[1]));
      stats.appendChild(node);
    });
    wrap.appendChild(stats);

    const chart = U.el('div', 'chart');
    chart.id = 'chart-flow';
    wrap.appendChild(chart);

    wrap.appendChild(flowTable(rows, s.tiered));
    return wrap;
  }

  function flowTable(rows, tiered) {
    const scroll = U.el('div', 'table-scroll');
    scroll.style.marginTop = '12px';
    const table = U.el('table', 'data-table');

    const cols = tiered
      ? ['日期', '收盘', '涨跌幅', '主力净额', '超大单', '大单', '中单', '小单', '主力占比']
      : ['日期', '收盘', '涨跌幅', '净流入', '超大单', '净占比'];

    const thead = U.el('thead');
    const htr = U.el('tr');
    cols.forEach(function (c) { htr.appendChild(U.el('th', '', c)); });
    thead.appendChild(htr);
    table.appendChild(thead);

    const tbody = U.el('tbody');
    rows.slice().reverse().forEach(function (r) {
      const tr = U.el('tr');
      tr.appendChild(U.el('td', '', r.date));
      tr.appendChild(U.el('td', '', U.price(r.close)));
      tr.appendChild(U.el('td', U.tone(r.change_pct), U.pct(r.change_pct)));
      tr.appendChild(U.el('td', U.tone(r.main), U.signedMoney(r.main)));
      tr.appendChild(U.el('td', U.tone(r.xl), U.signedMoney(r.xl)));
      if (tiered) {
        tr.appendChild(U.el('td', U.tone(r.lg), U.signedMoney(r.lg)));
        tr.appendChild(U.el('td', U.tone(r.md), U.signedMoney(r.md)));
        tr.appendChild(U.el('td', U.tone(r.sm), U.signedMoney(r.sm)));
      }
      tr.appendChild(U.el('td', U.tone(r.main_pct),
        U.isNum(r.main_pct) ? (r.main_pct > 0 ? '+' : '') + r.main_pct.toFixed(2) + '%' : U.NBSP));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    return scroll;
  }

  // ---------------------------------------------------------- 两融
  function marginSubtitle(d) {
    const s = d.margin.summary || {};
    if (!s.available) return '该股无两融数据';
    return s.days + ' 个交易日 · ' + s.sentiment;
  }

  function renderMargin(d) {
    const wrap = U.el('div');
    const s = d.margin.summary || {};
    const rows = d.margin.rows || [];

    if (!rows.length) {
      wrap.appendChild(U.el('div', 'loading-block',
        d.margin.error
          ? ('两融数据暂不可用：' + d.margin.error)
          : '该股不是两融标的，或暂无两融数据'));
      return wrap;
    }

    const stats = U.el('div', 'stat-row');
    [
      ['最新融资余额', U.money(s.rzye_last), ''],
      ['30日融资变动', U.signedMoney(s.rz_change), U.tone(s.rz_change)],
      ['30日变动幅度', U.pct(s.rz_change_pct), U.tone(s.rz_change_pct)],
      ['30日融资净买入', U.signedMoney(s.rz_net_total), U.tone(s.rz_net_total)],
      ['30日融资买入额', U.money(s.rz_buy_total), ''],
      ['最新融券余额', U.money(s.rqye_last), ''],
      ['30日融券变动', U.signedMoney(s.rq_change), U.tone(s.rq_change ? -s.rq_change : 0)],
      ['融资余额占比', U.isNum(s.rzyezb_last) ? s.rzyezb_last.toFixed(2) + '%' : U.NBSP, '']
    ].forEach(function (it) {
      const node = U.el('div', 'stat');
      node.appendChild(U.el('div', 'stat-label', it[0]));
      node.appendChild(U.el('div', 'stat-value ' + it[2], it[1]));
      stats.appendChild(node);
    });
    wrap.appendChild(stats);

    const c1 = U.el('div', 'chart chart-sm');
    c1.id = 'chart-margin';
    wrap.appendChild(c1);

    const c2 = U.el('div', 'chart chart-sm');
    c2.id = 'chart-margin-flow';
    wrap.appendChild(c2);

    wrap.appendChild(marginTable(rows));
    return wrap;
  }

  function marginTable(rows) {
    const scroll = U.el('div', 'table-scroll');
    scroll.style.marginTop = '12px';
    const table = U.el('table', 'data-table');

    const thead = U.el('thead');
    const htr = U.el('tr');
    ['日期', '融资余额', '融资买入额', '融资偿还额', '融资净买入', '融券余额', '融券余量(股)', '融券卖出量(股)', '两融余额']
      .forEach(function (c) { htr.appendChild(U.el('th', '', c)); });
    thead.appendChild(htr);
    table.appendChild(thead);

    const tbody = U.el('tbody');
    rows.slice().reverse().forEach(function (r) {
      const tr = U.el('tr');
      tr.appendChild(U.el('td', '', r.date));
      tr.appendChild(U.el('td', '', U.money(r.rzye)));
      tr.appendChild(U.el('td', '', U.money(r.rzmre)));
      tr.appendChild(U.el('td', '', U.money(r.rzche)));
      tr.appendChild(U.el('td', U.tone(r.rzjme), U.signedMoney(r.rzjme)));
      tr.appendChild(U.el('td', '', U.money(r.rqye)));
      tr.appendChild(U.el('td', '', U.money(r.rqyl, 0)));
      tr.appendChild(U.el('td', '', U.money(r.rqmcl, 0)));
      tr.appendChild(U.el('td', '', U.money(r.rzrqye)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    return scroll;
  }

  // ---------------------------------------------------------- 定期报告
  function financialsSubtitle(d) {
    const rows = (d.financials && d.financials.rows) || [];
    if (!rows.length) return '暂无季报/中报/年报数据';
    const prefix = (d.financials || {}).stale ? '数据源暂不可用，使用缓存 · ' : '';
    return prefix + '最新报告期 ' + (rows[0].period || rows[0].date || '--') + ' · ' + sourceLabel((d.financials || {}).source);
  }

  function financialPct(value) {
    return U.isNum(value) ? (value > 0 ? '+' : '') + value.toFixed(2) + '%' : U.NBSP;
  }

  function renderFinancials(d) {
    const pack = d.financials || {};
    const rows = pack.rows || [];
    const wrap = U.el('div');
    if (!rows.length) {
      wrap.appendChild(U.el('div', 'loading-block',
        pack.error ? ('财报数据暂不可用：' + pack.error) : '暂无可用季报/中报/年报数据'));
      return wrap;
    }

    const latest = rows[0];
    if (pack.stale) {
      wrap.appendChild(U.el('div', 'search-hint', '财报源暂不可用，以下为上次成功缓存数据；报告期和同比指标可能不是最新发布值。'));
    }
    const stats = U.el('div', 'stat-row');
    [
      ['最新报告期', latest.period || latest.date || '--', ''],
      ['营业收入', U.money(latest.revenue), ''],
      ['营收同比', financialPct(latest.revenue_yoy), U.tone(latest.revenue_yoy)],
      ['归母净利润', U.money(latest.net_profit), ''],
      ['净利润同比', financialPct(latest.net_profit_yoy), U.tone(latest.net_profit_yoy)],
      ['扣非净利润同比', financialPct(latest.net_profit_deduct_yoy), U.tone(latest.net_profit_deduct_yoy)],
      ['ROE', financialPct(latest.roe), U.tone(latest.roe)],
      ['资产负债率', financialPct(latest.debt_ratio), '']
    ].forEach(function (it) {
      const node = U.el('div', 'stat');
      node.appendChild(U.el('div', 'stat-label', it[0]));
      node.appendChild(U.el('div', 'stat-value ' + it[2], it[1]));
      stats.appendChild(node);
    });
    wrap.appendChild(stats);

    const scroll = U.el('div', 'table-scroll');
    scroll.style.marginTop = '12px';
    const table = U.el('table', 'data-table');
    const thead = U.el('thead');
    const htr = U.el('tr');
    ['报告期', '营业收入', '营收同比', '归母净利润', '净利润同比', '扣非同比', 'ROE', '负债率'].forEach(function (c) {
      htr.appendChild(U.el('th', '', c));
    });
    thead.appendChild(htr);
    table.appendChild(thead);
    const tbody = U.el('tbody');
    rows.slice(0, 8).forEach(function (r) {
      const tr = U.el('tr');
      tr.appendChild(U.el('td', '', r.period || r.date || '--'));
      tr.appendChild(U.el('td', '', U.money(r.revenue)));
      tr.appendChild(U.el('td', U.tone(r.revenue_yoy), financialPct(r.revenue_yoy)));
      tr.appendChild(U.el('td', '', U.money(r.net_profit)));
      tr.appendChild(U.el('td', U.tone(r.net_profit_yoy), financialPct(r.net_profit_yoy)));
      tr.appendChild(U.el('td', U.tone(r.net_profit_deduct_yoy), financialPct(r.net_profit_deduct_yoy)));
      tr.appendChild(U.el('td', U.tone(r.roe), financialPct(r.roe)));
      tr.appendChild(U.el('td', '', financialPct(r.debt_ratio)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    wrap.appendChild(scroll);
    wrap.appendChild(U.el('div', 'search-hint', '同比指标用于跨期比较；Q1/H1/Q3/FY 口径不同，不将报告期绝对值直接横比。'));
    return wrap;
  }

  // ---------------------------------------------------------- 数据源
  // 来源 id -> 中文名；同花顺 K 线来自其行情网页加载的数据文件（web 层），单独标注
  const SOURCE_NAME = {
    eastmoney: '东方财富',
    ths: '同花顺(网页)',
    sina: '新浪财经',
    tencent: '腾讯财经',
    akshare: 'AkShare',
    '': '未知'
  };

  function sourceLabel(id) {
    return SOURCE_NAME[id] || id || '未知';
  }

  function renderSourceFooter(d) {
    const s = d.sources || {};
    const node = U.el('div', 'search-hint');
    node.style.marginTop = '4px';
    const parts = [];
    if (s.quote) parts.push('行情:' + sourceLabel(s.quote));
    if (s.kline) parts.push('K线:' + sourceLabel(s.kline));
    if (s.fund_flow) parts.push('资金:' + sourceLabel(s.fund_flow));
    if (s.margin) parts.push('两融:' + sourceLabel(s.margin));
    if (s.financials) parts.push('财报:' + sourceLabel(s.financials));
    node.textContent = '数据来源 — ' + (parts.join(' | ') || '未知');
    const tip = U.el('div', 'search-hint');
    tip.style.marginTop = '2px';
    tip.style.color = 'var(--text-faint)';
    tip.style.fontSize = '11.5px';
    tip.textContent = '数据来自对应网站的网页公开数据，同花顺K线为其行情网页加载的数据文件；盘中行情为相应网站实时行情通道。';
    node.appendChild(document.createElement('br'));
    node.appendChild(tip);
    return node;
  }

  // ---------------------------------------------------------- 局部刷新
  // 只拉轻量行情（单只报价），避免每 3 秒整包重传 K线/资金/两融历史数据
  async function tick() {
    if (!state.data) return;
    // 盘后/节假日/用户手动停刷新时，session.auto_refresh=false，跳过本轮请求，
    // 避免每 5s 都打后端。session 由 API.quote 的响应持续同步，是权威来源。
    if (state.data.session && state.data.session.auto_refresh === false) return;
    if (state.ticking) return;            // 上一轮未回，跳过本轮，避免请求堆叠
    state.ticking = true;
    const seq = state.seq;
    try {
      const data = await API.quote(state.code, false);
      if (seq !== state.seq) return;      // 已切股票，丢弃
      if (!data || !data.quote) return;
      // 同步最新 session 到本地（盘后切换时 auto_refresh 会从 true 变 false）
      if (data.session) state.data.session = data.session;
      state.data.quote = data.quote;
      patchHead(data);
      App.setSession(data.session);
    } catch (err) { /* 静默 */ } finally {
      state.ticking = false;
    }
  }

  function patchHead(d) {
    const q = d.quote;
    const tone = U.tone(q.change_pct);
    const priceNode = document.querySelector('.detail-price');
    const chgNode = document.querySelector('.detail-change');
    if (priceNode) {
      priceNode.textContent = U.price(q.price);
      priceNode.className = 'detail-price ' + tone;
    }
    if (chgNode) {
      chgNode.textContent = (U.isNum(q.change) ? (q.change > 0 ? '+' : '') + q.change.toFixed(2) : U.NBSP)
        + '  ' + U.pct(q.change_pct);
      chgNode.className = 'detail-change ' + tone;
    }
  }

  global.PageDetail = {
    mount: mount,
    refresh: function () { return load(true); },
    tick: tick
  };
})(window);
