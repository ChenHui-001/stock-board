/* AI 分析弹窗（需求 6.x） */
(function (global) {
  'use strict';

  const ACTION_CLASS = {
    '积极持仓/加仓': 'act-buy',
    '持有观望': 'act-hold',
    '减仓规避': 'act-reduce',
    '清仓离场': 'act-sell'
  };

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
      + '<div class="ai-loading-sub">正在读取均线形态、30 日资金流向与两融数据…</div>'
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
    host.appendChild(verdict);

    // ---- 行情趋势分析
    host.appendChild(textSection('一、行情趋势分析', a.trend, [
      ['短期', 'short'], ['中期', 'mid'], ['中长期', 'long'], ['技术形态', 'pattern']
    ]));

    // ---- 资金与两融情绪
    host.appendChild(textSection('二、资金与两融情绪分析', a.capital, [
      ['主力资金', 'main_force'], ['散户情绪', 'retail'], ['两融多空', 'margin']
    ]));

    // ---- 风险与机会
    const risk = a.risk || {};
    const rs = U.el('div', 'ai-section');
    rs.appendChild(U.el('div', 'ai-section-title', '三、风险与机会拆解'));
    if ((risk.opportunities || []).length) {
      rs.appendChild(U.el('div', 'ai-item-label', '核心机会'));
      const ul = U.el('ul', 'ai-list good');
      risk.opportunities.forEach(function (x) { ul.appendChild(U.el('li', '', x)); });
      rs.appendChild(ul);
    }
    if ((risk.risks || []).length) {
      const lab = U.el('div', 'ai-item-label', '潜在风险');
      lab.style.marginTop = '9px';
      lab.style.display = 'block';
      rs.appendChild(lab);
      const ul = U.el('ul', 'ai-list bad');
      risk.risks.forEach(function (x) { ul.appendChild(U.el('li', '', x)); });
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
    lines.push('');

    const t = a.trend || {};
    lines.push('■ 行情趋势分析：' + (t.summary || ''));
    ['short:短期', 'mid:中期', 'long:中长期', 'pattern:技术形态'].forEach(function (pair) {
      const kv = pair.split(':');
      if (t[kv[0]]) lines.push('  ' + kv[1] + '：' + t[kv[0]]);
    });
    lines.push('');

    const c = a.capital || {};
    lines.push('■ 资金与两融情绪：' + (c.summary || ''));
    ['main_force:主力资金', 'retail:散户情绪', 'margin:两融多空'].forEach(function (pair) {
      const kv = pair.split(':');
      if (c[kv[0]]) lines.push('  ' + kv[1] + '：' + c[kv[0]]);
    });
    lines.push('');

    const r = a.risk || {};
    lines.push('■ 风险与机会：');
    (r.opportunities || []).forEach(function (x) { lines.push('  + ' + x); });
    (r.risks || []).forEach(function (x) { lines.push('  - ' + x); });
    lines.push('');
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
