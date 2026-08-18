/* AI 分析弹窗（需求 6.x） */
(function (global) {
  'use strict';

  const ACTION_CLASS = {
    '积极持仓/加仓': 'act-buy',
    '持有观望': 'act-hold',
    '减仓规避': 'act-reduce',
    '清仓离场': 'act-sell'
  };

  const RATING_CLASS = {
    '买入': 'rate-buy',
    '增持': 'rate-over',
    '中性': 'rate-flat',
    '减持': 'rate-bear',
    '卖出': 'rate-sell'
  };
  const SENT_CLASS = { '利好': 'sent-bull', '利空': 'sent-bear', '中性': 'sent-flat' };

  // 评级分布条（与研报页签一致）
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

  const state = { code: null, name: '', report: null, loading: false };

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
    state.report = null;

    titleNode().textContent = 'AI 智能分析 · ' + state.name + ' (' + code + ')';
    actionsNode().innerHTML = '';
    renderLoading();
    show();

    if (triggerBtn) {
      triggerBtn.disabled = true;
      triggerBtn.classList.add('loading');
    }
    state.loading = true;

    try {
      const report = await API.aiAnalyze(code, false);
      state.report = report;
      renderReport(report);
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

  async function reanalyze() {
    if (state.loading) return;
    state.loading = true;
    renderLoading();
    actionsNode().innerHTML = '';
    try {
      const report = await API.aiAnalyze(state.code, true);
      state.report = report;
      renderReport(report);
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
      + '<div class="ai-loading-text">AI 智能分析中，请稍候</div>'
      + '<div class="ai-loading-sub">正在读取当日实时盘口、均线形态、30 日资金流向与两融数据…</div>'
      + '</div>';
  }

  function renderError(msg) {
    body().innerHTML =
      '<div class="empty">'
      + '<div class="empty-icon">⚠️</div>'
      + '<div class="empty-title">分析失败</div>'
      + '<div class="empty-desc">' + U.escapeHtml(msg) + '</div>'
      + '</div>';
    const retry = U.el('button', 'btn btn-sm btn-primary', '重试');
    retry.onclick = reanalyze;
    actionsNode().innerHTML = '';
    actionsNode().appendChild(retry);
  }

  function renderReport(report) {
    const a = report.analysis || {};
    const adv = a.advice || {};
    const host = body();
    host.innerHTML = '';

    // ---- 结论卡片（重中之重，放最上面）
    const verdict = U.el('div', 'ai-verdict ' + (ACTION_CLASS[adv.action] || 'act-hold'));
    const vhead = U.el('div', 'ai-verdict-head');
    vhead.appendChild(U.el('div', 'ai-action', adv.action || '持有观望'));
    if (U.isNum(adv.confidence)) {
      vhead.appendChild(U.el('div', 'ai-conf', '置信度 ' + adv.confidence + '%'));
    }
    if (adv.horizon) vhead.appendChild(U.el('div', 'ai-conf', '周期 ' + adv.horizon));
    verdict.appendChild(vhead);

    if (adv.reason) verdict.appendChild(U.el('div', 'ai-reason', adv.reason));
    if (adv.position) verdict.appendChild(U.el('div', 'ai-position', '仓位建议：' + adv.position));
    if (adv.action_note) {
      verdict.appendChild(U.el('div', 'ai-position', '⚠ ' + adv.action_note));
    }

    const levels = U.el('div', 'ai-levels');
    [
      ['参考支撑位', U.price(adv.support)],
      ['参考压力位', U.price(adv.resistance)],
      ['介入区间', adv.entry_zone || U.NBSP],
      ['离场区间', adv.exit_zone || U.NBSP],
      ['止损位', U.price(adv.stop_loss)],
      ['止盈位', U.price(adv.take_profit)]
    ].forEach(function (l) {
      const node = U.el('div', 'ai-level');
      node.appendChild(U.el('div', 'l', l[0]));
      node.appendChild(U.el('div', 'v', l[1]));
      levels.appendChild(node);
    });
    verdict.appendChild(levels);

    // ---- 三维分面评分（技术面 / 资金面 / 消息面 + 信号一致性）
    const scores = adv.scores;
    if (scores && scores.tech != null) {
      const srow = U.el('div', 'ai-scores');
      const mk = function (label, v) {
        const node = U.el('span', 'ai-score ' + (v > 0 ? 'up' : v < 0 ? 'down' : 'flat'));
        node.appendChild(U.el('b', '', (v > 0 ? '+' : '') + v));
        node.appendChild(document.createTextNode(' ' + label));
        return node;
      };
      srow.appendChild(mk('技术面', scores.tech));
      srow.appendChild(mk('资金面', scores.capital));
      srow.appendChild(mk('消息面', scores.news));
      if (scores.intraday != null && scores.intraday !== 0) {
        // 当日盘口分项（盘中位置/量比/振幅/换手），已计入技术面
        const intra = U.el('span', 'ai-score intra ' + (scores.intraday > 0 ? 'up' : 'down'));
        intra.appendChild(U.el('b', '', (scores.intraday > 0 ? '+' : '') + scores.intraday));
        intra.appendChild(document.createTextNode(' 盘口'));
        intra.title = '当日盘口分项（盘中位置/量比/振幅/换手），已计入技术面';
        srow.appendChild(intra);
      }
      const sig = adv.signal;
      if (sig) {
        const sigNode = U.el('span', 'ai-signal ' + sig,
          sig === 'conflict' ? '⚠ 信号背离' : sig === 'aligned' ? '信号共振' : '信号中性');
        sigNode.title = adv.signal_note || '';
        srow.appendChild(sigNode);
      }
      verdict.appendChild(srow);
    }
    host.appendChild(verdict);

    // ---- 行情趋势分析（含当日盘中实时盘口）
    host.appendChild(textSection('一、行情趋势分析', a.trend, [
      ['当日盘中', 'intraday'], ['短期', 'short'], ['中期', 'mid'], ['中长期', 'long'], ['技术形态', 'pattern'], ['MACD/KDJ', 'oscillators']
    ]));

    // ---- 资金与两融情绪（含当日资金活跃）
    host.appendChild(textSection('二、资金与两融情绪分析', a.capital, [
      ['当日资金活跃', 'intraday'], ['主力资金', 'main_force'], ['散户情绪', 'retail'], ['两融多空', 'margin']
    ]));

    // ---- 风险与机会
    const risk = a.risk || {};
    const rs = U.el('div', 'ai-section');
    rs.appendChild(U.el('div', 'ai-section-title', '三、风险与机会拆解'));
    // 机会/风险条目兼容两种结构：纯字符串（旧格式）或 {text, strength, hit, note}
    // （盘口信号带历史命中率强度标注）
    function sigLi(x) {
      const li = U.el('li', '');
      if (typeof x === 'string') {
        li.textContent = x;
        return li;
      }
      li.appendChild(U.el('span', '', x.text || ''));
      if (x.strength) {
        const b = U.el('span', 'ai-sig-badge ai-sig-' + x.strength, x.strength);
        const conf = x.confidence || {};
        const confNote = conf.label ? '\n置信度: ' + conf.label + '（' + (conf.note || '') + '）' : '';
        b.title = (x.hit ? '历史命中率: ' + x.hit + '\n' : '') + (x.note || '') + confNote;
        li.appendChild(b);
        // 样本不足的信号（置信度低）整体弱化，与自检面板口径一致
        if (conf.level === 'low') li.classList.add('weak');
      }
      return li;
    }
    if ((risk.opportunities || []).length) {
      rs.appendChild(U.el('div', 'ai-item-label', '核心机会'));
      const ul = U.el('ul', 'ai-list good');
      risk.opportunities.forEach(function (x) { ul.appendChild(sigLi(x)); });
      rs.appendChild(ul);
    }
    if ((risk.risks || []).length) {
      const lab = U.el('div', 'ai-item-label', '潜在风险');
      lab.style.marginTop = '9px';
      lab.style.display = 'block';
      rs.appendChild(lab);
      const ul = U.el('ul', 'ai-list bad');
      risk.risks.forEach(function (x) { ul.appendChild(sigLi(x)); });
      rs.appendChild(ul);
    }
    host.appendChild(rs);

    // ---- 状态标签
    if ((report.status_tags || []).length) {
      const st = U.el('div', 'ai-section');
      st.appendChild(U.el('div', 'ai-section-title', '四、当前状态标签'));
      const tags = U.el('div', 'status-tags');
      report.status_tags.forEach(function (t) {
        const node = U.el('div', 'status-tag ' + (t.tone || 'flat'));
        node.appendChild(U.el('div', 'g', t.group));
        node.appendChild(U.el('div', 'v ' + (t.tone === 'up' ? 'up' : t.tone === 'down' ? 'down' : ''), t.label));
        tags.appendChild(node);
      });
      st.appendChild(tags);
      host.appendChild(st);
    }

    // ---- 券商研报面（情绪统计 + 最近关键研报，放最后）
    host.appendChild(reportSection(report));

    // ---- 元信息
    const meta = report.meta || {};
    const metaNode = U.el('div', 'ai-meta');
    metaNode.appendChild(U.el('span', '', '引擎：' + (meta.engine === 'llm' ? 'AI 大模型' : '内置规则引擎')));
    if (meta.model) metaNode.appendChild(U.el('span', '', '模型：' + meta.model));
    metaNode.appendChild(U.el('span', '', '生成时间：' + (report.cached_at || meta.generated_at || '--')));
    if (report.from_cache) metaNode.appendChild(U.el('span', '', '当日缓存结果'));
    host.appendChild(metaNode);

    if (meta.degraded_reason) {
      const warn = U.el('div', 'notice');
      warn.style.marginTop = '10px';
      warn.textContent = meta.degraded_reason;
      host.appendChild(warn);
    }

    host.appendChild(U.el('div', 'ai-disclaimer',
      '本分析基于公开行情数据由程序自动生成，不构成投资建议。市场有风险，决策请自行判断。'));

    // ---- 头部操作按钮
    const acts = actionsNode();
    acts.innerHTML = '';
    const copyBtn = U.el('button', 'btn btn-sm', '复制');
    copyBtn.onclick = async function () {
      const ok = await U.copyText(toPlainText(report));
      U.toast(ok ? '已复制到剪贴板' : '复制失败，请手动选择文本', ok ? 'ok' : 'err');
    };
    const againBtn = U.el('button', 'btn btn-sm btn-primary', '重新分析');
    againBtn.onclick = reanalyze;
    acts.appendChild(copyBtn);
    acts.appendChild(againBtn);
  }

  function reportSection(report) {
    const rsent = report.report_sentiment || {};
    const rprev = report.reports_preview || [];
    if (rsent.bull == null && !rprev.length) return document.createDocumentFragment();

    const sec = U.el('div', 'ai-section');
    sec.appendChild(U.el('div', 'ai-section-title', '券商研报面（近 30 日）'));

    // 情绪统计条：利好 / 利空 / 中性 + 评分
    const stats = U.el('div', 'report-stats');
    const mk = function (label, n, cls) {
      const node = U.el('span', 'report-stat ' + cls);
      node.appendChild(U.el('b', '', String(n)));
      node.appendChild(document.createTextNode(' ' + label));
      return node;
    };
    stats.appendChild(mk('利好', rsent.bull || 0, 'bull'));
    stats.appendChild(mk('利空', rsent.bear || 0, 'bear'));
    stats.appendChild(mk('中性', rsent.neutral || 0, 'flat'));
    if (rsent.score != null) {
      const sc = rsent.score;
      const scNode = U.el('span', 'report-score ' + (sc > 0 ? 'bull' : sc < 0 ? 'bear' : 'flat'));
      scNode.textContent = (sc > 0 ? '+' : '') + sc + ' 分';
      stats.appendChild(scNode);
    }
    sec.appendChild(stats);

    // 评级分布统计条（近一年，与研报页签一致）
    const distNode = renderRatingDist(report.rating_dist);
    if (distNode) sec.appendChild(distNode);

    // 最近关键研报
    if (rprev.length) {
      const ul = U.el('ul', 'report-list');
      rprev.forEach(function (r) {
        const li = U.el('li', 'report-item');
        const head = U.el('div', 'report-item-head');
        if (r.date) head.appendChild(U.el('span', 'report-item-date', r.date));
        if (r.source) head.appendChild(U.el('span', 'news-source', r.source));
        if (r.rating) {
          head.appendChild(U.el('span', 'rate-tag ' + (RATING_CLASS[r.rating] || 'rate-flat'), '评级 ' + r.rating));
        }
        const sent = r.sentiment || '中性';
        head.appendChild(U.el('span', 'sent-tag ' + (SENT_CLASS[sent] || 'sent-flat'), sent));
        li.appendChild(head);
        if (r.title) li.appendChild(U.el('div', 'report-item-title', r.title));
        ul.appendChild(li);
      });
      sec.appendChild(ul);
    }
    return sec;
  }

  function textSection(title, obj, fields) {
    const sec = U.el('div', 'ai-section');
    sec.appendChild(U.el('div', 'ai-section-title', title));
    if (!obj) {
      sec.appendChild(U.el('div', 'ai-item', '暂无数据'));
      return sec;
    }
    if (obj.summary) sec.appendChild(U.el('div', 'ai-summary', obj.summary));
    fields.forEach(function (f) {
      const val = obj[f[1]];
      if (!val) return;
      const item = U.el('div', 'ai-item');
      item.appendChild(U.el('span', 'ai-item-label', f[0] + '：'));
      item.appendChild(document.createTextNode(val));
      sec.appendChild(item);
    });
    return sec;
  }

  function toPlainText(report) {
    const a = report.analysis || {};
    const adv = a.advice || {};
    const lines = [];
    lines.push('【' + (report.name || '') + ' ' + report.code + '】AI 智能分析');
    lines.push('现价 ' + U.price(report.price) + '  涨跌幅 ' + U.pct(report.change_pct));
    lines.push('生成时间 ' + (report.cached_at || (report.meta || {}).generated_at || ''));
    lines.push('');
    lines.push('■ 持仓操作建议：' + (adv.action || ''));
    if (adv.reason) lines.push('  依据：' + adv.reason);
    if (adv.position) lines.push('  仓位：' + adv.position);
    lines.push('  支撑 ' + U.price(adv.support) + ' / 压力 ' + U.price(adv.resistance)
      + ' / 介入 ' + (adv.entry_zone || '--') + ' / 离场 ' + (adv.exit_zone || '--'));
    lines.push('  止损 ' + U.price(adv.stop_loss) + ' / 止盈 ' + U.price(adv.take_profit)
      + ' / 置信度 ' + (adv.confidence != null ? adv.confidence + '%' : '--'));
    const sc = adv.scores;
    if (sc && sc.tech != null) {
      lines.push('  三面评分：技术 ' + (sc.tech > 0 ? '+' : '') + sc.tech
        + ' / 资金 ' + (sc.capital > 0 ? '+' : '') + sc.capital
        + ' / 消息 ' + (sc.news > 0 ? '+' : '') + sc.news
        + (sc.intraday != null && sc.intraday !== 0 ? ' / 盘口 ' + (sc.intraday > 0 ? '+' : '') + sc.intraday + '（计入技术面）' : '')
        + (adv.signal === 'conflict' ? '（⚠ 信号背离，建议观望确认）' : ''));
    }
    lines.push('');

    const t = a.trend || {};
    lines.push('■ 行情趋势分析：' + (t.summary || ''));
    ['intraday:当日盘中', 'short:短期', 'mid:中期', 'long:中长期', 'pattern:技术形态', 'oscillators:MACD/KDJ'].forEach(function (pair) {
      const kv = pair.split(':');
      if (t[kv[0]]) lines.push('  ' + kv[1] + '：' + t[kv[0]]);
    });
    lines.push('');

    const c = a.capital || {};
    lines.push('■ 资金与两融情绪：' + (c.summary || ''));
    ['intraday:当日资金活跃', 'main_force:主力资金', 'retail:散户情绪', 'margin:两融多空'].forEach(function (pair) {
      const kv = pair.split(':');
      if (c[kv[0]]) lines.push('  ' + kv[1] + '：' + c[kv[0]]);
    });
    lines.push('');

    const r = a.risk || {};
    const txt = function (x) {
      if (typeof x === 'string') return x;
      return x.text + (x.strength ? ' 【' + x.strength + (x.hit ? '·' + x.hit : '') + '】' : '');
    };
    lines.push('■ 风险与机会：');
    (r.opportunities || []).forEach(function (x) { lines.push('  + ' + txt(x)); });
    (r.risks || []).forEach(function (x) { lines.push('  - ' + txt(x)); });
    lines.push('');

    // 研报面
    const rsent = report.report_sentiment || {};
    const rprev = report.reports_preview || [];
    if (rsent.bull != null || rprev.length) {
      lines.push('■ 券商研报面：');
      if (rsent.bull != null) {
        lines.push('  情绪统计：利好 ' + (rsent.bull || 0) + ' / 利空 ' + (rsent.bear || 0)
          + ' / 中性 ' + (rsent.neutral || 0)
          + (rsent.score != null ? '（计 ' + (rsent.score > 0 ? '+' : '') + rsent.score + ' 分）' : ''));
      }
      rprev.forEach(function (x) {
        lines.push('  [' + (x.date || '') + '] ' + (x.source || '')
          + (x.rating ? ' ' + x.rating : '') + '（' + (x.sentiment || '中性') + '）' + (x.title || ''));
      });
      lines.push('');
    }
    lines.push('（数据来源：公开行情接口；本内容不构成投资建议）');
    return lines.join('\n');
  }

  // 关闭交互
  document.addEventListener('click', function (e) {
    if (e.target && e.target.getAttribute && e.target.getAttribute('data-close') === '1') close();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !root().hidden) close();
  });

  global.AI = { open: open, close: close };
})(window);
