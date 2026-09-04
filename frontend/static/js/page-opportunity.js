/* page-opportunity —— 机会投资（短线策略选股页）
 *
 * 策略背景：docs/opportunity_strategy_v8.md（发酵+资金持续+分歧转强+妖股基因短线策略）
 * 架构完全对齐 page-value.js：#view 单容器 + pageGuard 防竞态 + skeleton 骨架 +
 * load/render/destroy/refresh/tick 模块钩子。
 * 渲染契约：任何子对象字段都可能为 null → 一律显示「—」；missing 数组渲染为
 * 「⚠ 数据缺失」小徽章；zt_prob / premium_prob 数值旁标注「模型估计」。
 */
import { U } from './util.js';
import { API } from './api.js';

// ---- 语义映射表 ----

// 市场情绪四档：A红 / B橙 / C黄 / D灰
const EMO_TONE = { A: 'opp-emo-a', B: 'opp-emo-b', C: 'opp-emo-c', D: 'opp-emo-d' };

// 板块周期五阶段徽章：发酵高亮 / 高潮橙 / 分歧蓝 / 启动灰 / 退潮红
function stageTone(stage) {
  if (stage === '发酵') return 'opp-hl';
  if (stage === '高潮') return 'opp-warn';
  if (stage === '分歧') return 'opp-blue';
  if (stage === '退潮') return 'opp-down';
  return 'opp-flat'; // 启动 / 未知
}

// 建议徽章：买入红底 / 观察黄底 / 放弃灰底
function actionTone(action) {
  if (action === '买入') return 'opp-act-buy';
  if (action === '观察') return 'opp-act-watch';
  return 'opp-act-skip';
}

// 分时结构徽章：A最优 / B分歧转强 / C谨慎 / D禁止买入
function minuteTone(structure) {
  if (structure === 'A') return 'opp-hl';
  if (structure === 'B') return 'opp-up';
  if (structure === 'C') return 'opp-warn';
  if (structure === 'D') return 'opp-down';
  return 'opp-flat';
}
const MINUTE_DESC = {
  A: 'A 级 · 最优结构',
  B: 'B 级 · 分歧转强',
  C: 'C 级 · 谨慎',
  D: 'D 级 · 禁止买入'
};

// 分歧转强勾选列表（五项）：null → —
const DIVERGENCE_ITEMS = [
  ['appeared', '出现分歧'],
  ['shrunk_volume', '缩量企稳'],
  ['fund_return', '资金回流'],
  ['breakout', '突破分歧高点'],
  ['back_above_vwap', '站回均价线']
];

// 交易计划两列键值（按契约顺序）
const PLAN_FIELDS = [
  ['buy_zone', '买入区间'],
  ['endgame_cond', '尾盘条件'],
  ['stop1', '第一止损'],
  ['stop2', '第二止损'],
  ['tp1', '第一止盈'],
  ['tp2', '第二止盈'],
  ['invalidate', '失效条件'],
  ['max_position', '最大仓位']
];

// ---- 通用格式化 ----

// null/undefined → 「—」，否则走指定格式化
function fmt(v, fn) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number' && !isFinite(v)) return '—';
  return fn ? fn(v) : v;
}
const num1 = function (v) { return v.toFixed(1); };
const num2 = function (v) { return v.toFixed(2); };
const pct1 = function (v) { return v.toFixed(1) + '%'; };
// 炸板率兼容小数(0.18)与百分数(18)两种口径
function ratePct(v) {
  if (v === null || v === undefined || !isFinite(v)) return '—';
  return (v <= 1 ? v * 100 : v).toFixed(1) + '%';
}

// 「⚠ 数据缺失」小徽章（黄底 chip）；空数组/缺字段返回 null
function missingBadges(list) {
  if (!list || !list.length) return null;
  const wrap = U.el('div', 'opp-missing-row');
  list.forEach(function (m) {
    wrap.appendChild(U.el('span', 'opp-missing', '⚠ 数据缺失：' + m));
  });
  return wrap;
}

// 通用小徽章
function chip(text, cls, title) {
  const c = U.el('span', 'opp-chip' + (cls ? ' ' + cls : ''), text);
  if (title) c.title = title;
  return c;
}

// ---- 市场状态卡 ----
function renderMarket(m) {
  if (!m) return null;
  const card = U.el('div', 'opp-card opp-market');

  const head = U.el('div', 'opp-market-head');
  const emo = chip(m.emotion || '—', 'opp-emo ' + (EMO_TONE[m.emotion] || 'opp-emo-d'),
    '市场情绪等级（A强势进攻 / B结构性机会 / C弱势轮动 / D风险退潮）');
  emo.classList.add('opp-emo-big');
  head.appendChild(emo);
  head.appendChild(U.el('span', 'opp-market-name', m.emotion_name || '—'));
  // 仓位区间
  const pos = U.el('span', 'opp-market-pos');
  pos.appendChild(document.createTextNode('建议仓位：'));
  pos.appendChild(U.el('b', '', (m.position || '—') + ' ' + (m.position_range || '')));
  head.appendChild(pos);
  card.appendChild(head);

  // 一行指标：涨停 / 炸板 / 跌停 / 最高连板 / 20cm活跃 / 炸板率
  const metrics = U.el('div', 'opp-metrics');
  const row = function (label, val, cls) {
    const item = U.el('div', 'opp-metric');
    item.appendChild(U.el('div', 'opp-metric-label', label));
    item.appendChild(U.el('div', 'opp-metric-value' + (cls ? ' ' + cls : ''), fmt(val)));
    return item;
  };
  metrics.appendChild(row('涨停', fmt(m.zt_count), 'up'));
  metrics.appendChild(row('炸板', fmt(m.zb_count), 'down'));
  metrics.appendChild(row('跌停', fmt(m.dt_count), 'down'));
  metrics.appendChild(row('最高连板', fmt(m.max_lianban)));
  metrics.appendChild(row('20cm活跃', fmt(m.active_20cm)));
  metrics.appendChild(row('炸板率', ratePct(m.zb_rate)));
  card.appendChild(metrics);

  const mb = missingBadges(m.missing);
  if (mb) card.appendChild(mb);
  return card;
}

// ---- 板块周期表 ----
function renderBoards(boards) {
  const wrap = U.el('div', 'opp-card');
  wrap.appendChild(U.el('div', 'opp-card-title', '板块周期'));
  if (!boards || !boards.length) {
    wrap.appendChild(U.el('div', 'opp-empty-line', '暂无板块数据'));
    return wrap;
  }
  const table = U.el('table', 'opp-table');
  const thead = U.el('thead');
  const trh = U.el('tr');
  ['板块', '评分', '阶段', '涨停数', '龙头', '今日主力', '5日主力', '数据缺失'].forEach(function (h) {
    trh.appendChild(U.el('th', '', h));
  });
  thead.appendChild(trh);
  table.appendChild(thead);
  const tbody = U.el('tbody');
  const mv = function (v) { return (v > 0 ? '+' : '') + v.toFixed(1) + '亿'; };
  boards.forEach(function (b) {
    const tr = U.el('tr', 'opp-row');
    tr.appendChild(U.el('td', 'opp-td-name', b.name || '—'));
    tr.appendChild(U.el('td', '', fmt(b.score, num1)));
    const tdStage = U.el('td');
    tdStage.appendChild(chip(b.stage || '—', stageTone(b.stage)));
    tr.appendChild(tdStage);
    tr.appendChild(U.el('td', '', fmt(b.zt_count)));
    tr.appendChild(U.el('td', '', b.has_leader ? '有' : '无'));
    // 板块主力净流入（亿元，正红负绿；缺失 → —）
    tr.appendChild(U.el('td', b.fund_today == null ? '' : U.tone(b.fund_today),
                        fmt(b.fund_today, mv)));
    tr.appendChild(U.el('td', b.fund_5d == null ? '' : U.tone(b.fund_5d),
                        fmt(b.fund_5d, mv)));
    const tdMiss = U.el('td', 'opp-td-miss');
    const mb = missingBadges(b.missing);
    if (mb) tdMiss.appendChild(mb);
    else tdMiss.textContent = '—';
    tr.appendChild(tdMiss);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

// ---- 候选卡片：评分行（综合分/妖股分/资金分/分歧转强/分时分/概率/R-R） ----
function renderScoreRow(root, c) {
  const row = U.el('div', 'opp-score-row');
  const item = function (label, val, cls, title) {
    const it = U.el('div', 'opp-score-item');
    it.appendChild(U.el('div', 'opp-score-label', label));
    const v = U.el('div', 'opp-score-value' + (cls ? ' ' + cls : ''));
    if (val instanceof Node) v.appendChild(val);
    else v.textContent = val;
    if (title) it.title = title;
    it.appendChild(v);
    return it;
  };
  // 综合分 + 星级
  const comp = U.el('span', '', fmt(c.composite, num1));
  if (c.stars) comp.appendChild(U.el('span', 'opp-stars', ' ' + c.stars));
  row.appendChild(item('综合分', comp, 'up'));
  row.appendChild(item('妖股分', fmt(c.yaogu && c.yaogu.score)));
  row.appendChild(item('资金分', fmt(c.fund && c.fund.score)));
  row.appendChild(item('分歧转强', fmt(c.divergence && c.divergence.score)));
  row.appendChild(item('分时分', fmt(c.minute && c.minute.score)));
  // 概率两枚都标注「模型估计」
  const zp = c.zt_prob || {};
  row.appendChild(item('涨停概率', fmt(zp.value, pct1), '', '模型估计，非真实概率'));
  if (zp.value != null) {
    const est = U.el('div', 'opp-score-est', '模型估计');
    row.lastChild.querySelector('.opp-score-value').appendChild(est);
  }
  const pp = c.premium_prob || {};
  row.appendChild(item('次日溢价', fmt(pp.value, pct1), '', '模型估计，非真实概率'));
  if (pp.value != null) {
    const est = U.el('div', 'opp-score-est', '模型估计');
    row.lastChild.querySelector('.opp-score-value').appendChild(est);
  }
  const rr = c.risk_reward || {};
  row.appendChild(item('R/R', fmt(rr.ratio, num2)));
  root.appendChild(row);
}

// ---- 候选卡片 ----
function renderCandidate(c) {
  const card = U.el('div', 'opp-card opp-cand');

  // 头部：rank / 名称代码 / 板块 / 现价涨跌 / 建议徽章
  const head = U.el('div', 'opp-cand-head');
  const left = U.el('div', 'opp-cand-id');
  left.appendChild(chip('#' + (c.rank != null ? c.rank : '?'), 'opp-rank'));
  const name = U.el('span', 'opp-cand-name', (c.name || '—') + ' ' + (c.code || ''));
  name.title = (c.code || '') + '.' + (c.market || '');
  left.appendChild(name);
  if (c.board) left.appendChild(chip(c.board, 'opp-flat'));
  head.appendChild(left);
  const right = U.el('div', 'opp-cand-quote');
  right.appendChild(U.el('span', 'opp-cand-price', fmt(c.price, num2)));
  if (c.change_pct != null) {
    right.appendChild(U.el('span', 'opp-cand-chg ' + U.tone(c.change_pct), U.pct(c.change_pct)));
  }
  head.appendChild(right);
  card.appendChild(head);
  // 建议徽章单独一行放头部下方（醒目）
  const actRow = U.el('div', 'opp-act-row');
  actRow.appendChild(chip('建议：' + (c.action || '—'), actionTone(c.action)));
  card.appendChild(actRow);

  // 指标网格（2 行 4 列）：换手/量比/成交额/流通市值 + 主净/主净比/5日/20日
  const grid = U.el('div', 'opp-metrics opp-metrics-grid');
  const cell = function (label, val, cls) {
    const item = U.el('div', 'opp-metric');
    item.appendChild(U.el('div', 'opp-metric-label', label));
    item.appendChild(U.el('div', 'opp-metric-value' + (cls ? ' ' + cls : ''), val));
    return item;
  };
  grid.appendChild(cell('换手', fmt(c.turnover, pct1)));
  grid.appendChild(cell('量比', fmt(c.volume_ratio, num2)));
  grid.appendChild(cell('成交额', fmt(c.amount, function (v) { return v.toFixed(1) + '亿'; })));
  grid.appendChild(cell('流通市值', fmt(c.float_mv, function (v) { return v.toFixed(0) + '亿'; })));
  grid.appendChild(cell('主力净流入', fmt(c.main_net_inflow, function (v) { return U.money(v); })));
  grid.appendChild(cell('主力净比', fmt(c.main_net_pct, pct1)));
  grid.appendChild(cell('5日涨幅', fmt(c.chg5, U.pct), U.tone(c.chg5)));
  grid.appendChild(cell('20日涨幅', fmt(c.chg20, U.pct), U.tone(c.chg20)));
  card.appendChild(grid);

  // 评分行
  renderScoreRow(card, c);

  // 三重准入徽章（gates 全 true 才允许推荐）
  const gates = c.gates || {};
  const gateRow = U.el('div', 'opp-gates');
  const gate = function (ok, label) {
    if (ok === true) return chip(label + ' ✓', 'opp-up', label + '门槛通过');
    if (ok === false) return chip(label + ' ✗', 'opp-down', label + '门槛未通过');
    return chip(label + ' —', 'opp-flat', label + '门槛数据缺失');
  };
  gateRow.appendChild(U.el('span', 'opp-gates-title', '三重准入'));
  gateRow.appendChild(gate(gates.board, '板块'));
  gateRow.appendChild(gate(gates.stock, '个股'));
  gateRow.appendChild(gate(gates.prob, '概率'));
  card.appendChild(gateRow);

  // 板块信息行：阶段 + 评分 + 相对大盘强度
  const bi = c.board_info;
  if (bi) {
    const brow = U.el('div', 'opp-sub-row');
    brow.appendChild(U.el('span', 'opp-sub-label', '所属板块'));
    brow.appendChild(chip(bi.stage || '—', stageTone(bi.stage)));
    brow.appendChild(U.el('span', 'opp-sub-item', '评分 ' + fmt(bi.score, num1)));
    brow.appendChild(chip('涨停 ' + fmt(bi.zt_count), 'opp-flat'));
    if (bi.has_leader) brow.appendChild(chip('有龙头', 'opp-hl'));
    if (bi.is_ferment) brow.appendChild(chip('发酵中', 'opp-hl'));
    if (U.isNum(bi.relative_strength)) {
      const beat = bi.relative_strength >= 0;
      brow.appendChild(U.el('span', 'opp-sub-item ' + (beat ? 'up' : 'down'),
        (beat ? '跑赢大盘 ' : '跑输大盘 ') + Math.abs(bi.relative_strength).toFixed(1) + '%'));
    }
    card.appendChild(brow);
    const bmb = missingBadges(bi.missing);
    if (bmb) card.appendChild(bmb);
  }

  // 分时结构徽章
  const mi = c.minute;
  if (mi && (mi.structure || mi.structure_name)) {
    const mrow = U.el('div', 'opp-sub-row');
    mrow.appendChild(U.el('span', 'opp-sub-label', '分时结构'));
    mrow.appendChild(chip(MINUTE_DESC[mi.structure] || (mi.structure || '—'),
      minuteTone(mi.structure)));
    if (mi.structure_name) mrow.appendChild(U.el('span', 'opp-sub-item', mi.structure_name));
    card.appendChild(mrow);
  }

  // 妖股基因明细（小表格：名称 got/max + note）
  const yg = c.yaogu;
  if (yg && yg.items && yg.items.length) {
    const sec = U.el('div', 'opp-sec');
    sec.appendChild(U.el('div', 'opp-sec-title', '妖股基因明细'));
    const table = U.el('table', 'opp-table opp-table-sm');
    yg.items.forEach(function (it) {
      const tr = U.el('tr', 'opp-row');
      tr.appendChild(U.el('td', 'opp-td-name', it.name || '—'));
      tr.appendChild(U.el('td', 'opp-td-got', fmt(it.got) + ' / ' + fmt(it.max)));
      tr.appendChild(U.el('td', 'opp-td-note', it.note || '—'));
      tbody(tr, table);
    });
    sec.appendChild(table);
    card.appendChild(sec);
  }
  function tbody(tr, table) {
    if (!table.querySelector('tbody')) table.appendChild(U.el('tbody'));
    table.querySelector('tbody').appendChild(tr);
  }

  // 资金明细：当日/3日/5日/30日主力净流入 + 超大单/大单当日
  const fd = c.fund;
  if (fd) {
    const sec = U.el('div', 'opp-sec');
    sec.appendChild(U.el('div', 'opp-sec-title', '资金明细'));
    const grid = U.el('div', 'opp-metrics opp-metrics-grid');
    const flow = function (label, v) {
      const item = U.el('div', 'opp-metric');
      item.appendChild(U.el('div', 'opp-metric-label', label));
      item.appendChild(U.el('div', 'opp-metric-value ' + U.tone(v), fmt(v, U.signedMoney)));
      return item;
    };
    grid.appendChild(flow('当日主力', fd.day));
    grid.appendChild(flow('3日主力', fd.day3));
    grid.appendChild(flow('5日主力', fd.day5));
    grid.appendChild(flow('30日主力', fd.day30));
    grid.appendChild(flow('超大单当日', fd.xl_today));
    grid.appendChild(flow('大单当日', fd.lg_today));
    sec.appendChild(grid);
    card.appendChild(sec);
    const fmb = missingBadges(fd.missing);
    if (fmb) card.appendChild(fmb);
  }

  // 分歧转强勾选列表
  const dv = c.divergence;
  if (dv) {
    const sec = U.el('div', 'opp-sec');
    sec.appendChild(U.el('div', 'opp-sec-title', '分歧转强'));
    const marks = U.el('div', 'opp-checklist');
    DIVERGENCE_ITEMS.forEach(function (pair) {
      const v = dv[pair[0]];
      // true ✓ / false ✗ / null —
      const cls = v === true ? 'ok' : (v === false ? 'no' : 'na');
      const sym = v === true ? '✓' : (v === false ? '✗' : '—');
      marks.appendChild(U.el('span', 'opp-check ' + cls, pair[1] + ' ' + sym));
    });
    sec.appendChild(marks);
    card.appendChild(sec);
    const dmb = missingBadges(dv.missing);
    if (dmb) card.appendChild(dmb);
  }

  // 概率依据（小字）
  const zp = c.zt_prob || {}, pp = c.premium_prob || {};
  if (zp.basis || pp.basis) {
    const sec = U.el('div', 'opp-sec');
    sec.appendChild(U.el('div', 'opp-sec-title', '概率依据（模型估计，非真实概率）'));
    if (zp.basis) sec.appendChild(U.el('div', 'opp-basis', '涨停概率：' + zp.basis));
    if (pp.basis) sec.appendChild(U.el('div', 'opp-basis', '次日溢价：' + pp.basis));
    card.appendChild(sec);
  }

  // 交易计划表（两列键值）
  const plan = c.plan;
  if (plan) {
    const sec = U.el('div', 'opp-sec');
    sec.appendChild(U.el('div', 'opp-sec-title', '交易计划'));
    const table = U.el('table', 'opp-table opp-plan');
    PLAN_FIELDS.forEach(function (pair) {
      const tr = U.el('tr', 'opp-row');
      tr.appendChild(U.el('td', 'opp-plan-key', pair[1]));
      tr.appendChild(U.el('td', 'opp-plan-val', plan[pair[0]] != null && plan[pair[0]] !== '' ? plan[pair[0]] : '—'));
      table.appendChild(tr);
    });
    sec.appendChild(table);
    card.appendChild(sec);
  }

  // 结论段：action_reason 原文
  if (c.action_reason) {
    card.appendChild(U.el('div', 'opp-conclusion', c.action_reason));
  }

  // 卡片底部 missing 徽章
  const cmb = missingBadges(c.missing);
  if (cmb) card.appendChild(cmb);

  return card;
}

// ---- empty 大横幅 ----
function renderEmptyBanner(reason) {
  const ban = U.el('div', 'opp-empty-banner');
  ban.appendChild(U.el('div', 'opp-empty-title', '【今日无符合条件标的，空仓优于强行交易】'));
  if (reason) ban.appendChild(U.el('div', 'opp-empty-reason', reason));
  ban.appendChild(U.el('div', 'opp-empty-sub',
    '宁可错过不做低质量交易 · 三重准入缺一不可 · 板块/个股/概率任一未达标即不推荐'));
  return ban;
}

// ---- 数据渲染总入口 ----
function renderData(data) {
  skeleton();
  const root = viewEl.querySelector('.page-opportunity');

  if (data.generated_at) {
    root.appendChild(U.el('div', 'opp-gen',
      '生成于 ' + data.generated_at + (state.refreshing ? '' : ' · 缓存10分钟')));
  }

  const mkt = renderMarket(data.market);
  if (mkt) root.appendChild(mkt);

  const boards = renderBoards(data.boards);
  if (boards) root.appendChild(boards);

  // 候选区标题
  root.appendChild(U.el('div', 'opp-section-title', '今日最终候选'));

  // empty=true → 大横幅；否则渲染候选卡片（后端保证 ≤2 只）
  if (data.empty) {
    root.appendChild(renderEmptyBanner(data.empty_reason || ''));
  } else if (data.candidates && data.candidates.length) {
    data.candidates.forEach(function (c) { root.appendChild(renderCandidate(c)); });
  } else {
    root.appendChild(U.el('div', 'opp-empty-line', '暂无候选数据'));
  }
}

// ---- 骨架（含页头与刷新按钮，渲染完成后由 renderData 重建） ----
function skeleton() {
  viewEl.innerHTML = '';
  const root = U.el('div', 'page-opportunity');
  const head = U.el('div', 'opp-head');
  const title = U.el('h2', 'opp-title');
  title.appendChild(U.icon('bolt', { size: 16 }));
  title.appendChild(document.createTextNode(' 机会投资'));
  head.appendChild(title);
  const right = U.el('div', 'opp-head-right');
  const refresh = U.el('button', 'btn btn-sm btn-primary', '强刷重算');
  refresh.title = '跳过 10 分钟缓存，重新计算候选（较慢）';
  refresh.onclick = async function () {
    if (state.refreshing) return;
    state.refreshing = true;
    refresh.textContent = '重算中…';
    refresh.disabled = true;
    try {
      state.data = await API.opportunity(true);
      state.error = null;
      renderData(state.data);
      U.toast('已强制重算机会候选', 'ok');
    } catch (e) {
      state.error = e.message || String(e);
      viewEl.innerHTML = '';
      viewEl.appendChild(U.el('div', 'opp-error', '加载失败: ' + state.error));
    } finally {
      state.refreshing = false;
      if (refresh.isConnected) {
        refresh.textContent = '强刷重算';
        refresh.disabled = false;
      }
    }
  };
  right.appendChild(refresh);
  head.appendChild(right);
  root.appendChild(head);
  viewEl.appendChild(root);
}

// 防竞态守卫：await 期间用户切页时丢弃旧结果（与 page-value 同机制）
const pageGuard = U.createPageGuard();

async function load() {
  if (!viewEl) return;
  const my = pageGuard.begin();
  skeleton();
  state.loading = true;
  try {
    state.data = await API.opportunity(false);
    if (!pageGuard.ok(my)) return;
    state.error = null;
    renderData(state.data);
  } catch (e) {
    if (!pageGuard.ok(my)) return;
    state.error = e.message || String(e);
    viewEl.innerHTML = '';
    viewEl.appendChild(U.el('div', 'opp-error', '加载失败: ' + state.error));
  } finally {
    if (pageGuard.ok(my)) state.loading = false;
  }
}

// ---- Module API ----
const state = {
  data: null,
  loading: false,
  refreshing: false,
  error: null
};

let viewEl = null;

export const PageOpportunity = {
  mount: function () {
    viewEl = document.getElementById('view');
    load();
  },
  destroy: function () {
    // 路由切换卸载钩子（app.js 在切页前调用）
    pageGuard.kill();
    viewEl = null;
  },
  refresh: async function () {
    // 顶栏「刷新」按钮：走缓存拉取（10 分钟内不重复计算）
    if (!viewEl) return;
    const my = pageGuard.begin();
    state.data = await API.opportunity(false);
    if (!pageGuard.ok(my)) return;
    renderData(state.data);
  },
  tick: function () {
    // 后端 10 分钟缓存，tick 只在加载出错时轻提示
    if (state.error) U.toast('机会投资数据加载异常: ' + state.error, 'warn');
  }
};
