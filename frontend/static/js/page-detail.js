/* page-detail（从 IIFE+global 转 ESM） */
import { U } from './util.js';
import { API } from './api.js';
import { Charts } from './charts.js';
import { AI } from './ai.js';
import { App } from './app.js';


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
    view().innerHTML = renderSkeleton();
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

  // 加载骨架屏：顶部标题/价格 + 行情宫格 + 两个内容块占位，减少“白屏等数据”的跳变
  function renderSkeleton() {
    return '<div class="detail-skeleton" aria-label="加载中">'
      + '<div class="sk-block sk-title"></div>'
      + '<div class="sk-block sk-price"></div>'
      + '<div class="sk-grid">'
      +   '<div class="sk-block sk-cell"></div>'
      +   '<div class="sk-block sk-cell"></div>'
      +   '<div class="sk-block sk-cell"></div>'
      +   '<div class="sk-block sk-cell"></div>'
      + '</div>'
      + '<div class="sk-block sk-body"></div>'
      + '<div class="sk-block sk-body"></div>'
      + '</div>';
  }

  function render() {
    const d = state.data;
    const root = view();
    Charts.disposeAll();
    root.innerHTML = '';

    root.appendChild(renderHead(d));

    // P0-7：AI 结论卡片前置（异步填充，骨架先出，数据到后填充；默认只读缓存 + 规则快算）
    root.appendChild(renderAiSummaryCard(d));

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

    // P0-7：异步拉取 AI 结论（不阻塞首屏渲染）
    requestAnimationFrame(function () { loadAiSummaryAsync(d); });

    // 图表要在 DOM 挂载后再初始化
    requestAnimationFrame(function () {
      Charts.maChart(document.getElementById('chart-ma'), d.kline, d.ma_summary.series, 'detail-charts');
      const flowRows = d.fund_flow.rows;
      if (flowRows && flowRows.length) {
        Charts.flowChart(document.getElementById('chart-capital-flow'), flowRows, !!d.fund_flow.summary.tiered, 'detail-charts');
      }
      const marginRows = d.margin.rows;
      if (marginRows && marginRows.length) {
        Charts.marginChart(document.getElementById('chart-capital-margin-main'), marginRows, 'detail-charts');
        Charts.marginFlowChart(document.getElementById('chart-capital-margin-flow'), marginRows, 'detail-charts');
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
    // 返回按钮:从哪儿来回哪儿去(home/value/hotspot/search),无来源时 fallback 到 home。
    // App.backLabel() / App.goBack() 在 app.js 中实现,详情页不再硬编码目标。
    const back = U.el('button', 'btn btn-sm', App.backLabel());
    back.onclick = function () { App.goBack(); };
    // hash 变化时(同页面内跳转到其他股票)不重渲按钮;只在 mount 时设定即可。
    nav.appendChild(back);

    const refreshBtn = U.el('button', 'btn btn-sm', '');
    refreshBtn.title = '强制刷新本股全部数据';
    refreshBtn.appendChild(U.el('span', 'refresh-icon', '⟳'));
    refreshBtn.appendChild(document.createTextNode(' 刷新'));
    refreshBtn.onclick = function () {
      if (refreshBtn.disabled) return;
      refreshBtn.disabled = true;
      refreshBtn.classList.add('spinning');
      load(true).finally(function () {
        refreshBtn.classList.remove('spinning');
        refreshBtn.disabled = false;
      });
    };
    nav.appendChild(refreshBtn);

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

  // ---------------------------------------------------------- P0-4 分时决策面板
  /**
   * 5 格：VWAP / 量比 / 主力净比 / 板块情绪 / 相对板块
   * 每格三层：label + value(主) + sub(辅助说明 tone)
   * 盘中实时随 5s tick 局部更新（仅 patch 文本节点，不重渲整块）
   * 数据缺失按 PRD §4.4 降级规则显示「—」+ 原因，不塌陷整块
   *
   * 板块情绪判定（PRD §6.2 草案 v1）：
   *   退潮 / 高潮 / 发酵 / 启动 / 震荡（5 态）
   *   阈值取自 PRD §6.2 草案，待用户校准（PRD §Q2）
   */
  function boardEmotionCycle(boardChgPct) {
    if (!U.isNum(boardChgPct)) return { stage: '', tone: '' };
    const v = boardChgPct;
    if (v <= -1.5) return { stage: '退潮', tone: 'cycle-fout' };
    if (v >= 3.0)  return { stage: '高潮', tone: 'cycle-peak' };
    if (v >= 1.0)  return { stage: '发酵', tone: 'cycle-rise' };
    if (v >= 0)    return { stage: '启动', tone: 'cycle-start' };
    return          { stage: '震荡', tone: 'cycle-flat' };
  }

  /**
   * 相对板块强弱：当前股涨跌幅 - 主板块涨跌幅
   * 优先取第一个有 change_pct 的板块（sl 顺序：行业 → 概念 → 地域伪板块，详见 PRD §Q1）
   */
  function relativeStrength(quote, boards) {
    const qChg = quote.change_pct;
    if (!U.isNum(qChg) || !Array.isArray(boards)) return null;
    const skipSuffix = ['板块', 'HS', '上证', '深证', 'MSCI', '富时'];
    const real = boards.filter(function (b) {
      if (!b || !U.isNum(b.change_pct)) return false;
      const n = b.name || '';
      return !skipSuffix.some(function (s) { return n.startsWith(s) || n.indexOf(s) >= 0; });
    });
    const refBoard = real[0] || boards.find(function (b) { return b && U.isNum(b.change_pct); });
    if (!refBoard || !U.isNum(refBoard.change_pct)) return null;
    return { diff: qChg - refBoard.change_pct, refName: refBoard.name || '' };
  }

  function renderIntradayPanel(d) {
    const card = U.el('div', 'card intraday-panel');
    card.id = 'intraday-panel';

    const head = U.el('div', 'card-head');
    head.appendChild(U.el('div', 'card-title', '分时决策面板'));
    head.appendChild(U.el('div', 'card-sub', '盘中实时 · 数据空时显示「—」+ 原因，不塌陷整块'));
    card.appendChild(head);

    const grid = U.el('div', 'intraday-grid');

    // 第 1 格：VWAP
    const vwap = d.quote.vwap;
    const dev = d.quote.deviation_pct;
    const vwapCell = U.el('div', 'intraday-cell');
    vwapCell.appendChild(U.el('div', 'intraday-label', 'VWAP 分时均价'));
    vwapCell.appendChild(U.el('div', 'intraday-value',
      U.isNum(vwap) ? '¥' + vwap.toFixed(2) : U.NBSP));
    const vwapSub = U.el('div', 'intraday-sub');
    if (U.isNum(dev)) {
      const devCls = dev > 0.1 ? 'up' : dev < -0.1 ? 'down' : 'flat';
      vwapSub.classList.add(devCls);
      vwapSub.textContent = (dev > 0 ? '+' : '') + dev.toFixed(2) + '% · '
        + (dev > 0 ? '均价上方' : dev < 0 ? '均价下方' : '贴近均价');
    } else {
      vwapSub.textContent = '数据缺失';
    }
    vwapCell.appendChild(vwapSub);
    grid.appendChild(vwapCell);

    // 第 2 格：量比
    const vr = d.quote.volume_ratio;
    const vrTier = volumeRatioTier(vr);
    const vrCell = U.el('div', 'intraday-cell');
    vrCell.appendChild(U.el('div', 'intraday-label', '量比'));
    vrCell.appendChild(U.el('div', 'intraday-value ' + vrTier.tone,
      U.isNum(vr) ? vr.toFixed(2) : U.NBSP));
    vrCell.appendChild(U.el('div', 'intraday-sub', vrTier.hint || '—'));
    grid.appendChild(vrCell);

    // 第 3 格：主力净比
    const mp = d.quote.main_net_pct;
    let mpTone = '', mpLabel = '';
    if (U.isNum(mp)) {
      if (mp > 5)        { mpTone = 'mp-huge-up';  mpLabel = '大幅流入'; }
      else if (mp > 2)   { mpTone = 'mp-strong-up'; mpLabel = '流入'; }
      else if (mp > -2)  { mpTone = 'mp-flat';      mpLabel = '平衡'; }
      else if (mp > -5)  { mpTone = 'mp-mild-dn';  mpLabel = '流出'; }
      else               { mpTone = 'mp-huge-dn';  mpLabel = '大幅流出'; }
    }
    const mpCell = U.el('div', 'intraday-cell');
    mpCell.appendChild(U.el('div', 'intraday-label', '主力净比'));
    mpCell.appendChild(U.el('div', 'intraday-value ' + mpTone,
      U.isNum(mp) ? (mp > 0 ? '+' : '') + mp.toFixed(2) + '%' : U.NBSP));
    mpCell.appendChild(U.el('div', 'intraday-sub', mpLabel || '—'));
    grid.appendChild(mpCell);

    // 第 4 格：板块情绪
    const boards = (d.boards_detail && d.boards_detail.length) ? d.boards_detail : [];
    const firstBoardChg = boards.length && U.isNum(boards[0].change_pct) ? boards[0].change_pct : null;
    const cycle = boardEmotionCycle(firstBoardChg);
    const cycCell = U.el('div', 'intraday-cell');
    cycCell.appendChild(U.el('div', 'intraday-label', '板块情绪'));
    cycCell.appendChild(U.el('div', 'intraday-value ' + cycle.tone,
      cycle.stage || U.NBSP));
    const cycSub = U.el('div', 'intraday-sub');
    if (boards[0]) {
      const n = boards[0].name || '';
      const chg = U.isNum(boards[0].change_pct) ? (boards[0].change_pct > 0 ? '+' : '') + boards[0].change_pct.toFixed(2) + '%' : '—';
      cycSub.textContent = n + (chg !== '—' ? ' ' + chg : '');
    } else {
      cycSub.textContent = '板块数据缺失';
    }
    cycCell.appendChild(cycSub);
    grid.appendChild(cycCell);

    // 第 5 格：相对板块
    const rs = relativeStrength(d.quote, boards);
    const rsCell = U.el('div', 'intraday-cell');
    rsCell.appendChild(U.el('div', 'intraday-label', '相对板块'));
    let rsTone = '';
    if (rs && U.isNum(rs.diff)) {
      rsTone = rs.diff > 0.3 ? 'up' : rs.diff < -0.3 ? 'down' : 'flat';
      rsCell.appendChild(U.el('div', 'intraday-value ' + rsTone,
        (rs.diff > 0 ? '+' : '') + rs.diff.toFixed(2) + '%'));
      rsCell.appendChild(U.el('div', 'intraday-sub',
        (rs.diff > 0 ? '强于' : rs.diff < 0 ? '弱于' : '同步') + ' ' + (rs.refName || '板块')));
    } else {
      rsCell.appendChild(U.el('div', 'intraday-value flat', U.NBSP));
      rsCell.appendChild(U.el('div', 'intraday-sub', '板块数据缺失'));
    }
    grid.appendChild(rsCell);

    card.appendChild(grid);
    return card;
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

  /**
   * 量比分层（PRD §P0-6 验收）：
   *   <0.5  极度缩量  /  0.5–0.8 缩量  /  0.8–1.2 正常
   *   1.2–2.0 温和放量  /  2.0–5.0 显著放量  /  >5.0 巨量
   * 返回 cell 类名 +  简短副标题，前端 CSS 给不同底色。
   */
  function volumeRatioTier(v) {
    if (!U.isNum(v)) return { tone: '', hint: '' };
    if (v < 0.5)  return { tone: 'vol-extreme-shrink', hint: '极度缩量' };
    if (v < 0.8)  return { tone: 'vol-shrink',         hint: '缩量' };
    if (v < 1.2)  return { tone: 'vol-flat',           hint: '正常' };
    if (v < 2.0)  return { tone: 'vol-mild-up',        hint: '温和放量' };
    if (v < 5.0)  return { tone: 'vol-strong-up',      hint: '显著放量' };
    return        { tone: 'vol-huge-up',      hint: '巨量' };
  }

  function renderQuoteGrid(d) {
    const q = d.quote;
    const grid = U.el('div', 'quote-grid');
    const vr = q.volume_ratio;
    const vrTier = volumeRatioTier(vr);
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

    // P0-6：量比上详情页（独立一格，分层高亮 + 副标题）
    const vrCell = U.el('div', 'quote-cell');
    vrCell.appendChild(U.el('div', 'quote-label', '量比'));
    const vrBox = U.el('div', 'quote-value ' + vrTier.tone,
                       U.isNum(vr) ? vr.toFixed(2) : U.NBSP);
    if (vrTier.hint) vrBox.appendChild(document.createTextNode(''));
    vrCell.appendChild(vrBox);
    if (vrTier.hint) {
      vrCell.appendChild(U.el('div', 'quote-sub', vrTier.hint));
    }
    grid.appendChild(vrCell);

    // P0-1：VWAP 偏离（amount/volume 盘中实时，前端已经在响应里，UI 早该上墙）
    const dev = q.deviation_pct;
    const devTone = !U.isNum(dev) ? '' : (dev > 0 ? 'up' : dev < 0 ? 'down' : 'flat');
    const devCell = U.el('div', 'quote-cell');
    devCell.appendChild(U.el('div', 'quote-label', 'VWAP 偏离'));
    devCell.appendChild(U.el('div', 'quote-value ' + devTone,
                             U.isNum(dev) ? (dev > 0 ? '+' : '') + dev.toFixed(2) + '%' : U.NBSP));
    if (U.isNum(dev)) {
      const tip = dev > 0 ? '现价在分时均价上方运行（强势）'
                 : dev < 0 ? '现价在分时均价下方运行（弱势）' : '现价贴近均价';
      devCell.title = tip;
    }
    grid.appendChild(devCell);

    // P0-2：主力净比（f184）盘中实时可得，原页面没显示过
    const mp = q.main_net_pct;
    const mpTone = !U.isNum(mp) ? '' : (mp > 0 ? 'up' : mp < 0 ? 'down' : 'flat');
    const mpCell = U.el('div', 'quote-cell');
    mpCell.appendChild(U.el('div', 'quote-label', '主力净比'));
    mpCell.appendChild(U.el('div', 'quote-value ' + mpTone,
                            U.isNum(mp) ? (mp > 0 ? '+' : '') + mp.toFixed(2) + '%' : U.NBSP));
    grid.appendChild(mpCell);

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

  // ---------------------------------------------------------- P0-7 AI 结论卡片（前置 + 异步）
  /**
   * 卡片结构：
   *   ▸ 头部：action 大字（如「加仓」红色 / 「减仓」绿色 / 「观望」灰色）
   *   ▸ 副头：置信度档位（低/中/高 配色）+ 引擎来源（规则/LLM）+ 数据时间
   *   ▸ 触发条件（triggers.text）：加仓/减仓具体价位 + 价格止损位 + 时间止损
   *   ▸ 常驻免责说明（按 P0-8 同步落地）
   *   ▸ 「查看完整 AI 报告 →」按钮，复用现有 ai.open() 弹窗
   *
   * 异步填充策略（PRD §Q6 建议默认不触发 LLM）：
   *   1. 卡片先出骨架（loading）
   *   2. 并行：① 读缓存（GET /api/analysis/{code}）② 规则快算（POST /api/analysis）
   *      （规则永远比 LLM 快，先到先用）
   *   3. 用户点「完整报告」才触发 LLM（POST /api/analysis?force=true）
   */
  function renderAiSummaryCard(d) {
    const card = U.el('div', 'card ai-summary-card');
    card.id = 'ai-summary-card';
    card.dataset.code = d.quote.code;
    card.dataset.name = d.quote.name || '';

    const head = U.el('div', 'card-head');
    head.appendChild(U.el('div', 'card-title', '🤖 AI 智能分析'));
    head.appendChild(U.el('div', 'card-sub', '首屏直接给出三选一建议，点击下方展开完整 LLM 报告'));
    card.appendChild(head);

    const body = U.el('div', 'card-body ai-summary-body');
    // 骨架：3 行占位（action + 置信 + triggers）
    body.appendChild(U.el('div', 'ai-summary-skeleton', '分析中…'));
    card.appendChild(body);

    // 「查看完整报告」按钮
    const actions = U.el('div', 'ai-summary-actions');
    const btn = U.el('button', 'btn btn-sm btn-ghost', '查看完整 AI 报告 →');
    btn.onclick = function () {
      import('./ai.js').then(function (mod) {
        mod.AI.open(d.quote.code, d.quote.name, btn);
      });
    };
    actions.appendChild(btn);
    card.appendChild(actions);

    return card;
  }

  /**
   * 异步拉取并填充 AI 结论：先打规则快算（有缓存就用缓存），永远不自动触发 LLM。
   * 失败/超时 → 卡片显示「数据准备中，可点击下方按钮生成完整报告」并不再重试。
   */
  async function loadAiSummaryAsync(d) {
    const card = document.getElementById('ai-summary-card');
    if (!card) return;
    const body = card.querySelector('.ai-summary-body');
    if (!body) return;

    // 先打 POST /api/ai/{code}（refresh=false 即只读缓存，不触发 LLM）
    try {
      const res = await fetch('/api/ai/' + encodeURIComponent(d.quote.code), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({})  // refresh 默认 false
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      // 响应形状: { analysis: { advice: {...}, ... }, engine, model, ... }
      const advice = (data && data.analysis && data.analysis.advice) || (data && data.advice) || {};
      if (advice.action) {
        _fillAiSummaryBody(body, advice, data);
        return;
      }
    } catch (e) {
      // 静默：失败时卡片保留骨架态，按钮仍可用
    }

    // 兜底路径取消：规则快算的接口与缓存读取共用 /api/ai/{code}，
    // 上一步失败 = 无缓存 + LLM 不可用 = 保留骨架即可。
      if (res.ok) {
        const data = await res.json();
        const advice = (data && data.advice) || {};
        if (advice.action) {
          _fillAiSummaryBody(body, advice, data);
          return;
        }
      }
    } catch (e) {}

    // 全部失败：保留骨架，按钮提示用户手动触发 LLM
    body.innerHTML = '';
    body.appendChild(U.el('div', 'ai-summary-empty',
      '数据准备中，可点击下方「查看完整 AI 报告 →」生成结论'));
  }

  /**
   * 把 advice 渲染进卡片 body：
   *   - action 大字 + 颜色（act-buy/red / act-reduce/green / act-hold/gray）
   *   - 置信度档位 + 来源（规则引擎 / LLM）+ 数据时间
   *   - 触发条件（含具体价格）+ 时间止损
   *   - 常驻免责说明
   */
  function _fillAiSummaryBody(body, advice, meta) {
    body.innerHTML = '';
    const tone = { '加仓': 'act-buy', '减仓': 'act-reduce', '观望': 'act-hold' };
    const cls = tone[advice.action] || 'act-hold';

    // 头部一行：action 大字 + 置信度档位 + 引擎来源 + 数据时间
    const head = U.el('div', 'ai-summary-head ' + cls);
    head.appendChild(U.el('div', 'ai-summary-action', advice.action));

    if (U.isNum(advice.confidence)) {
      const c = advice.confidence;
      const tier = c <= 45 ? { label: '低', cls: 'conf-low' }
                : c <= 64 ? { label: '中', cls: 'conf-mid' }
                :           { label: '高', cls: 'conf-high' };
      const confNode = U.el('span', 'ai-conf ' + tier.cls,
                            '置信度 ' + tier.label + '(' + c + '%)');
      if (advice.confidence_reason) confNode.title = advice.confidence_reason;
      head.appendChild(confNode);
    }
    const srcParts = [];
    if (meta && meta.engine === 'llm') srcParts.push('LLM');
    else srcParts.push('规则引擎');
    if (meta && meta.data_time) srcParts.push('数据 ' + meta.data_time);
    head.appendChild(U.el('div', 'ai-summary-source', srcParts.join(' · ')));
    body.appendChild(head);

    // 触发条件（具体价格 + 止损 + 时间止损）
    const trig = advice.triggers || {};
    const trigBox = U.el('div', 'ai-summary-triggers');
    if (trig.text) {
      trigBox.appendChild(U.el('div', 'ai-summary-trigger-row', trig.text));
    }
    if (advice.stop_loss_text) {
      trigBox.appendChild(U.el('div', 'ai-summary-trigger-row', advice.stop_loss_text));
    }
    if (advice.time_stop) {
      trigBox.appendChild(U.el('div', 'ai-summary-trigger-row',
        '⏱ 时间止损：' + advice.time_stop));
    }
    body.appendChild(trigBox);

    // 仓位建议（一句话）
    if (advice.position) {
      body.appendChild(U.el('div', 'ai-summary-position', advice.position));
    }

    // 常驻免责
    body.appendChild(U.el('div', 'ai-disclaimer',
      '⚠ 规则引擎评分方向性有限，置信度仅表示信号一致程度，不作收益承诺'));
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

  // statGroup(title, items, mode?)
  //   mode='grid'    (默认): 卡片网格,适合短值(label 上 value 下)
  //   mode='list':          每项一行 label: value 的紧凑列表,适合长值(带来源说明等)
  //   mode='compact':       真正横向布局,所有指标排成一行,label 左 / value 右,
  //                         value 超长时单行省略号,悬停 title 看完整内容。适合"区间与位置"、
  //                         "趋势与波幅"这类 4~6 项、含"次要支撑/压力"等多源值的统计组。
  //                         第 4 项 s[3] 可选,提供作为 value 完整文本的 tooltip。
  function statGroup(title, items, mode) {
    const m = mode || 'grid';
    const group = U.el('div', 'stat-group mode-' + m);
    group.appendChild(U.el('div', 'stat-group-title', title));
    const row = U.el('div', 'stat-row mode-' + m);
    items.forEach(function (s) {
      let node;
      if (m === 'compact') {
        node = U.el('div', 'stat-compact');
        const labelNode = U.el('span', 'stat-compact-label', s[0]);
        const valueNode = U.el('span', 'stat-compact-value' + (s[2] ? ' ' + s[2] : ''), s[1]);
        if (s[3]) valueNode.title = s[3];
        node.appendChild(labelNode);
        node.appendChild(valueNode);
      } else {
        node = U.el('div', 'stat' + (m === 'list' ? ' stat-inline' : ''));
        node.appendChild(U.el('div', 'stat-label', s[0]));
        node.appendChild(U.el('div', 'stat-value' + (s[2] ? ' ' + s[2] : ''), s[1]));
      }
      row.appendChild(node);
    });
    group.appendChild(row);
    return group;
  }

  // 横向紧凑布局下,多源值(次要支撑/压力)只显示第一项 + 省略号,完整列表放 s[3] 给 tooltip。
  // 返回 [text, fullText] 两元组,前者用于展示,后者用于悬停。
  function _compactSecondary(list) {
    if (!Array.isArray(list) || list.length === 0) return [U.NBSP, ''];
    const parts = list.map(function (item) {
      const p = U.price(item.price);
      return p + (item.from ? ' (' + item.from + ')' : '');
    });
    const full = parts.join(' / ');
    const show = parts.length > 1 ? parts[0] + ' …' : full;
    return [show, full];
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
    // 分组呈现：区间与位置 / 趋势与波幅，避免 8 个指标无差别平铺
    // 横向紧凑布局：4 项一行,超长 value 单行省略 + title 看完整。
    // 次要支撑/压力保留"主值 + 省略号",hover 看其余来源。
    wrap.appendChild(statGroup('区间与位置', [
      ['20日区间', U.price(sr.low_20) + ' ~ ' + U.price(sr.high_20), '', ''],
      ['60日区间', U.price(sr.low_60) + ' ~ ' + U.price(sr.high_60), '', ''],
      (function () {
        const sec = _compactSecondary(sr.secondary_support);
        return ['次要支撑', sec[0], '', sec[1]];
      })(),
      (function () {
        const sec = _compactSecondary(sr.secondary_resistance);
        return ['次要压力', sec[0], '', sec[1]];
      })()
    ], 'compact'));
    // 波幅单位 ATR：把固定百分比阈值换成"相当于多少倍 ATR"，避免高波动股票永远被判震荡
    wrap.appendChild(statGroup('趋势与波幅', [
      ['近5日涨跌', U.pct(d.status.trend.chg_5d), '', ''],
      ['近20日涨跌', U.pct(d.status.trend.chg_20d), '', ''],
      ['近60日涨跌', U.pct(d.status.trend.chg_60d), '', ''],
      ['近5日波幅', U.isNum(trend5) ? trend5.toFixed(2) + ' 个ATR' : U.NBSP, vol5Tone, '']
    ], 'compact'));
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

  export const PageDetail = {
    mount: mount,
    refresh: function () { return load(true); },
    tick: tick
  };
