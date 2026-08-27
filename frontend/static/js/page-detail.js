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

    root.appendChild(section('当前股票状态', renderStatus(d), '整合均线、资金、两融数据输出的标准化状态标签', 'status'));
    root.appendChild(section('均线数据（MA5 / MA10 / MA20 / MA60）', renderMa(d), maSubtitle(d), 'ma'));
    root.appendChild(section('资金与杠杆（30 天）', renderCapital(d), capitalSubtitle(d), 'capital'));
    root.appendChild(section('财报数据（季报 / 中报 / 三季报 / 年报）', renderFinancials(d), financialsSubtitle(d), 'financials'));
    root.appendChild(renderSourceFooter(d));

    // 图表要在 DOM 挂载后再初始化
    requestAnimationFrame(function () {
      Charts.maChart(document.getElementById('chart-ma'), d.kline, d.ma_summary.series);
      const flowRows = d.fund_flow.rows;
      if (flowRows && flowRows.length) {
        Charts.flowChart(document.getElementById('chart-capital-flow'), flowRows, !!d.fund_flow.summary.tiered);
      }
      const marginRows = d.margin.rows;
      if (marginRows && marginRows.length) {
        Charts.marginChart(document.getElementById('chart-capital-margin-main'), marginRows);
        Charts.marginFlowChart(document.getElementById('chart-capital-margin-flow'), marginRows);
      }
    });
  }

  function notice(text, kind) {
    return U.el('div', 'notice' + (kind ? ' ' + kind : ''), text);
  }

  function section(title, body, subtitle, anchor) {
    const card = U.el('div', 'card section');
    if (anchor) card.id = 'section-' + anchor;
    card.dataset.anchor = anchor || '';
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

    // 关键指标条：一眼可见的「支撑·压力·区间位置·ATR·资金状态·两融情绪」，
    // 紧凑横排，减少下拉翻找。桌面 6 列、平板 3 列、手机 2 列。
    wrap.appendChild(renderKeyMetrics(d));

    // ----- 数据时间戳 + 5秒刷新倒计时 -----
    wrap.appendChild(renderDataStamp(d));

    // ----- 紧凑行情 grid：原 '基础行情' section 内联到顶部，避免一次点击后还要滚动才能看到今开/成交 -----
    wrap.appendChild(renderQuoteGrid(d));

    // ----- sticky 锚点导航：长页面下快速跳转 -----
    wrap.appendChild(renderAnchorNav());

    return wrap;
  }

  // ----- 锚点导航：点击平滑跳转，滚动时高亮当前 section -----
  let _anchorObserver = null;
  function renderAnchorNav() {
    const nav = U.el('div', 'detail-anchor');
    nav.id = 'detail-anchor';
    const items = [
      { key: 'status', label: '状态' },
      { key: 'ma', label: '均线' },
      { key: 'capital', label: '资金与杠杆' },
      { key: 'financials', label: '财报' }
    ];
    items.forEach(function (it) {
      const a = U.el('a', 'detail-anchor-item', it.label);
      a.href = '#section-' + it.key;
      a.dataset.target = it.key;
      a.onclick = function (e) {
        e.preventDefault();
        const target = document.getElementById('section-' + it.key);
        if (target) {
          const top = target.getBoundingClientRect().top + window.scrollY - 70;
          window.scrollTo({ top: top, behavior: 'smooth' });
        }
      };
      nav.appendChild(a);
    });
    // IntersectionObserver: 跳转时高亮当前 section
    setTimeout(function () { setupAnchorObserver(items); }, 100);
    return nav;
  }

  function setupAnchorObserver(items) {
    if (_anchorObserver) {
      _anchorObserver.disconnect();
      _anchorObserver = null;
    }
    if (!('IntersectionObserver' in window)) return;
    const navLinks = document.querySelectorAll('#detail-anchor .detail-anchor-item');
    _anchorObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          const key = e.target.dataset.anchor;
          navLinks.forEach(function (a) {
            a.classList.toggle('active', a.dataset.target === key);
          });
        }
      });
    }, { rootMargin: '-70px 0px -60% 0px', threshold: 0 });
    items.forEach(function (it) {
      const sec = document.getElementById('section-' + it.key);
      if (sec) _anchorObserver.observe(sec);
    });
  }

  // ----- 数据时间戳：交易日期 + 服务端/客户端获取时间 + 5秒刷新倒计时 -----
  let _detailMountedAt = 0;
  let _detailIntervalMs = 0;
  let _detailTimerHandle = null;
  let _detailLastFetchAt = 0;
  function renderDataStamp(d) {
    const wrap = U.el('div', 'detail-stamp');
    const sess = d.session || {};
    _detailIntervalMs = sess.interval_ms || 0;
    _detailMountedAt = Date.now();
    _detailLastFetchAt = Date.now();
    const q = d.quote || {};
    const tradeDate = q.trade_date || '--';
    const fetchTime = U.fmtTime(new Date());
    const tsNode = U.el('div', 'detail-stamp-info');
    tsNode.appendChild(U.el('span', '', '交易日期 ' + tradeDate));
    tsNode.appendChild(U.el('span', '', '· 获取 ' + fetchTime));
    if (sess.label) tsNode.appendChild(U.el('span', '', '· ' + sess.label));
    wrap.appendChild(tsNode);
    if (_detailIntervalMs > 0) {
      const cd = U.el('div', 'detail-stamp-countdown');
      cd.textContent = '距下次刷新 ' + (_detailIntervalMs / 1000).toFixed(0) + 's';
      wrap.appendChild(cd);
      if (_detailTimerHandle) clearInterval(_detailTimerHandle);
      _detailTimerHandle = setInterval(function () {
        const elapsed = Date.now() - _detailLastFetchAt;
        const remain = Math.max(0, _detailIntervalMs - elapsed) / 1000;
        cd.textContent = '距下次刷新 ' + remain.toFixed(1) + 's';
        if (remain <= 0.1) {
          _detailLastFetchAt = Date.now();
          if (typeof tick === 'function') tick(true);
        }
      }, 200);
    } else {
      const cd = U.el('div', 'detail-stamp-countdown static', '盘后手动刷新');
      wrap.appendChild(cd);
    }
    return wrap;
  }

  // ---------------------------------------------------------- 关键指标条
  function renderKeyMetrics(d) {
    const sr = d.support_resistance || {};
    const fs = d.fund_flow.summary || {};
    const ms = d.margin.summary || {};
    const ts = d.status.trend || {};
    const rangePos = U.isNum(sr.range_pos_pct) ? sr.range_pos_pct.toFixed(0) + '%' : U.NBSP;
    const rangeTone = U.isNum(sr.range_pos_pct) ? (sr.range_pos_pct >= 50 ? 'up' : 'down') : '';
    const atrTone = sr.atr_breakout === '已突破' ? 'up'
      : sr.atr_breakout === '已跌破' ? 'down' : '';
    const fundTone = fs.state_grade === '强' || fs.state_grade === '中' ? 'up'
      : fs.state_grade === '弱' || fs.state_grade === '溃' ? 'down' : '';
    const marginTone = ms.sentiment && ms.sentiment.indexOf('偏多') >= 0 ? 'up'
      : ms.sentiment && ms.sentiment.indexOf('偏空') >= 0 ? 'down' : '';

    // 主支撑/主压力/区间位置/ATR 突破 是最关键的 4 个信息；资金/两融改放 Phase 2 合并区
    const cells = [
      {
        label: '主支撑', value: U.price(sr.support),
        sub: sr.support_from ? '来源 ' + sr.support_from : '',
        tone: '', tip: '当前 0.5×ATR 容差下的有效支撑位；下方失位警示'
      },
      {
        label: '主压力', value: U.price(sr.resistance),
        sub: sr.resistance_from ? '来源 ' + sr.resistance_from : '',
        tone: '', tip: '当前 0.5×ATR 容差下的有效压力位；突破是趋势确认'
      },
      {
        label: '区间位置', value: rangePos, sub: '近 20 日', tone: rangeTone,
        tip: '现价在 20 日区间内的相对位置；≥50% 偏多、<50% 偏空'
      },
      {
        label: 'ATR(14)', value: U.isNum(sr.atr) ? sr.atr.toFixed(2) : U.NBSP,
        sub: sr.atr_breakout || '—', tone: atrTone,
        tip: '近 14 日平均真实波幅；突破/跌破用 0.5×ATR 容差判定'
      }
    ];

    const row = U.el('div', 'detail-keymetrics');
    cells.forEach(function (c) {
      const cell = U.el('div', 'km-cell');
      if (c.tip) cell.title = c.tip;
      cell.appendChild(U.el('div', 'km-label', c.label));
      cell.appendChild(U.el('div', 'km-value ' + c.tone, c.value));
      cell.appendChild(U.el('div', 'km-sub', c.sub));
      row.appendChild(cell);
    });
    return row;
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
  function _formatSecondaryList(list) {
    // 把后端给定的 [{price, from}] 列表渲染为「X.XX (来源) / Y.YY (来源)」，
    // 最多 3 项。空数组返回空格占位。
    if (!Array.isArray(list) || list.length === 0) return U.NBSP;
    return list.map(function (item) {
      const p = U.price(item.price);
      return p + (item.from ? ' (' + item.from + ')' : '');
    }).join(' / ');
  }

  function renderStatus(d) {
    const wrap = U.el('div');
    // 盘中背离警告条：后端 build_status 在 60 分线趋势与日线背离时
    // 返回 divergence_hint（aligned=False），前端据此在 status 顶部加醒目的黄横幅
    if (d.status.divergence_hint) {
      const banner = U.el('div', 'status-warn-banner', d.status.divergence_hint);
      wrap.appendChild(banner);
    }
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
    // ATR 突破判定染色：突破/跌破偏警示（up=绿已突破向上 / down=红已跌破向下）
    // 逼近与未触及中性，不染。
    const breakTone = sr.atr_breakout === '已突破' ? 'up'
      : sr.atr_breakout === '已跌破' ? 'down' : '';
    // 近 5 日波幅 (单位 ATR)：>1.0 偏强(up)，<-1.0 偏弱(down)
    const trend5 = (d.status.trend.vol_unit_atr || {}).chg_5d;
    const vol5Tone = U.isNum(trend5)
      ? (trend5 >= 1 ? 'up' : trend5 <= -1 ? 'down' : '')
      : '';
    [
      ['20日区间', U.price(sr.low_20) + ' ~ ' + U.price(sr.high_20)],
      ['60日区间', U.price(sr.low_60) + ' ~ ' + U.price(sr.high_60)],
      // ATR(14)：用 0.5 倍 ATR 作为突破容差，比固定 ±0.5% 更贴合个股波动；
      // atr_breakout 用文字描述当前与区间的位置关系
      // P1-4：次要支撑/压力，让用户看到"下一个位置"
      ['次要支撑', _formatSecondaryList(sr.secondary_support), ''],
      ['次要压力', _formatSecondaryList(sr.secondary_resistance), ''],
      ['近5日涨跌', U.pct(d.status.trend.chg_5d)],
      ['近20日涨跌', U.pct(d.status.trend.chg_20d)],
      ['近60日涨跌', U.pct(d.status.trend.chg_60d)],
      // 波幅单位 ATR：把固定百分比阈值换成"相当于多少倍 ATR"，避免高波动股票永远被判震荡
      ['近5日波幅', U.isNum(trend5) ? trend5.toFixed(2) + ' 个ATR' : U.NBSP, vol5Tone]
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
    // 资金区副标题：把「价量背离」「主力类型」「连涨天数」等专业操盘信号展示出来，
    // 让用户一眼看到当前资金动作的性质——高位诱多/低位吸筹/机构主导/连涨天数等。
    // 数据缺失时跳过该片段，保持简短可读。
    const parts = [s.days + ' 个交易日', s.trend, s.state];
    if (s.streak_text) parts.push(s.streak_text);
    if (s.price_flow_note) parts.push(s.price_flow_note);
    if (s.xl_dominance) parts.push(s.xl_dominance);
    return parts.join(' · ');
  }

  // 当日资金流向未发布（盘中 / 收盘后 16 点前）：东财/新浪日级资金流向通常
  // 16 点后才有当日数据，此时最后一行是前一交易日，必须明确标注日期避免误导
  function flowFreshNotice(d) {
    const s = d.fund_flow.summary || {};
    if (!s || !s.available || s.fresh) return '';
    return '当日资金流向尚未发布（通常收盘后 16 点更新），以下展示最近交易日 '
      + (s.last_date || '--') + ' 数据；状态判定已退回近5日口径。';
  }

  // ----- 资金与杠杆合并区副标题：把两边的核心状态拼成一句话 -----
  function capitalSubtitle(d) {
    const fs = d.fund_flow.summary || {};
    const ms = d.margin.summary || {};
    const parts = [];
    if (fs.available) parts.push(fs.days + ' 个交易日 资金');
    if (ms.available) parts.push(ms.days + ' 个交易日 两融');
    if (fs.trend) parts.push(fs.trend);
    if (ms.sentiment) parts.push(ms.sentiment);
    return parts.join(' · ');
  }

  // ----- 资金与杠杆：左侧资金流向 + 右侧两融的双列布局 -----
  function renderCapital(d) {
    const wrap = U.el('div');
    const hasFlow = (d.fund_flow.rows || []).length > 0;
    const hasMargin = (d.margin.rows || []).length > 0;

    // 双列容器
    const cols = U.el('div', 'capital-cols');

    // 左列：资金流向
    const left = U.el('div', 'capital-col');
    left.appendChild(U.el('div', 'capital-col-title', '资金流向'));
    if (!hasFlow) {
      left.appendChild(U.el('div', 'loading-block',
        d.fund_flow.error ? ('资金流向暂不可用：' + d.fund_flow.error) : '暂无资金流向数据'));
    } else {
      const fs = d.fund_flow.summary || {};
      const stats = U.el('div', 'stat-row');
      const lastLabel = fs.fresh ? '当日主力' : '最近交易日主力';
      [
        ['30日主力净额', U.signedMoney(fs.main_total), U.tone(fs.main_total)],
        ['近5日主力', U.signedMoney(fs.main_last5), U.tone(fs.main_last5)],
        [lastLabel, U.signedMoney(fs.main_last), U.tone(fs.main_last)],
        ['超大单合计', U.signedMoney(fs.xl_total), U.tone(fs.xl_total)]
      ].forEach(function (it) {
        const node = U.el('div', 'stat');
        node.appendChild(U.el('div', 'stat-label', it[0]));
        node.appendChild(U.el('div', 'stat-value ' + it[2], it[1]));
        stats.appendChild(node);
      });
      left.appendChild(stats);
      const freshNotice = flowFreshNotice(d);
      if (freshNotice) left.appendChild(notice(freshNotice, 'warn'));
      if (!fs.tiered) {
        left.appendChild(notice(
          '当前资金数据来自备用源（' + (d.fund_flow.source || '未知') + '），'
          + '仅提供净流入与超大单口径，无四档拆分。', 'info'));
      }
      const flowChart = U.el('div', 'chart');
      flowChart.id = 'chart-capital-flow';
      flowChart.style.marginTop = '12px';
      left.appendChild(flowChart);
      left.appendChild(flowTable(d.fund_flow.rows, fs.tiered));
    }
    cols.appendChild(left);

    // 右列：两融
    const right = U.el('div', 'capital-col');
    right.appendChild(U.el('div', 'capital-col-title', '两融数据'));
    if (!hasMargin) {
      right.appendChild(U.el('div', 'loading-block',
        d.margin.error ? ('两融数据暂不可用：' + d.margin.error) : '该股不是两融标的，或暂无两融数据'));
    } else {
      const ms = d.margin.summary || {};
      const stats = U.el('div', 'stat-row');
      [
        ['最新融资余额', U.money(ms.rzye_last), ''],
        ['30日融资变动', U.signedMoney(ms.rz_change), U.tone(ms.rz_change)],
        ['30日变动幅度', U.pct(ms.rz_change_pct), U.tone(ms.rz_change_pct)],
        ['30日融资净买入', U.signedMoney(ms.rz_net_total), U.tone(ms.rz_net_total)],
        ['最新融券余额', U.money(ms.rqye_last), ''],
        ['融资占流通市值', U.isNum(ms.rzyezb_last) ? ms.rzyezb_last.toFixed(2) + '%' : U.NBSP, '']
      ].forEach(function (it) {
        const node = U.el('div', 'stat');
        node.appendChild(U.el('div', 'stat-label', it[0]));
        node.appendChild(U.el('div', 'stat-value ' + it[2], it[1]));
        stats.appendChild(node);
      });
      right.appendChild(stats);
      const c1 = U.el('div', 'chart chart-sm');
      c1.id = 'chart-capital-margin-main';
      c1.style.marginTop = '12px';
      right.appendChild(c1);
      const c2 = U.el('div', 'chart chart-sm');
      c2.id = 'chart-capital-margin-flow';
      right.appendChild(c2);
      right.appendChild(marginTable(d.margin.rows));
    }
    cols.appendChild(right);

    wrap.appendChild(cols);
    return wrap;
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
    chart.id = 'chart-capital-flow';
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
    c1.id = 'chart-capital-margin-main';
    wrap.appendChild(c1);

    const c2 = U.el('div', 'chart chart-sm');
    c2.id = 'chart-capital-margin-flow';
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
    // ----- 核心指标卡：一眼可见 4 个最关键指标 -----
    const coreCards = U.el('div', 'fin-core');
    const coreItems = [
      { label: '营收同比', v: latest.revenue_yoy, fmt: financialPct, tone: U.tone(latest.revenue_yoy),
        tip: '报告期营业收入同比变化；越高越佳' },
      { label: '净利同比', v: latest.net_profit_yoy, fmt: financialPct, tone: U.tone(latest.net_profit_yoy),
        tip: '归母净利润同比变化；越高越佳' },
      { label: 'ROE', v: latest.roe, fmt: financialPct, tone: U.tone(latest.roe),
        tip: '净资产收益率；≥10% 偏强，<0% 偏弱' },
      { label: '负债率', v: latest.debt_ratio, fmt: financialPct, tone: '',
        tip: '资产负债率；≥70% 需警惕财务杠杆' }
    ];
    coreItems.forEach(function (c) {
      const card = U.el('div', 'fin-core-card');
      if (c.tip) card.title = c.tip;
      card.appendChild(U.el('div', 'fin-core-label', c.label));
      const valueNode = U.el('div', 'fin-core-value ' + (c.tone || ''), c.fmt(c.v));
      card.appendChild(valueNode);
      // 额外提示：负债率高低需反向呈现
      if (c.label === '负债率' && U.isNum(c.v)) {
        const warn = U.el('div', 'fin-core-warn',
          c.v >= 70 ? '高负债' : c.v <= 30 ? '低负债' : '中等负债');
        card.appendChild(warn);
      }
      coreCards.appendChild(card);
    });
    wrap.appendChild(coreCards);

    const stats = U.el('div', 'stat-row');
    [
      ['最新报告期', latest.period || latest.date || '--', ''],
      ['营业收入', U.money(latest.revenue), ''],
      ['归母净利润', U.money(latest.net_profit), ''],
      ['扣非净利同比', financialPct(latest.net_profit_deduct_yoy), U.tone(latest.net_profit_deduct_yoy)]
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
