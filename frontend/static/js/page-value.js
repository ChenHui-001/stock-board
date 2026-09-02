/* page-value（从 IIFE+global 转 ESM） */
import { U } from './util.js';
import { API } from './api.js';


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

  // 信号 → 详细解释（鼠标悬停的 tooltip 文案）
  const SIGNAL_DESC = {
    VALUE_BUY: 'PE<15 深度低估 + 基本面稳健 + 风险低 → 「黄金坑」买点，慢富长持之选。',
    QUALITY_HOLD: '综合 80+ 且 PE≤30 → 「长期持有」白马,基本面与估值双正。',
    BUY: '综合分达标、量价/资金/板块配合到位 → 「建议买入」持仓跟入。',
    BREAKOUT_BUY: '放量启动 + 连板异动 → 「突破买入」短线情绪博弈,严设止损。',
    PULLBACK_BUY: '分歧/低吸期 → 「回调低吸」右侧入场点。',
    WATCH: '总分 60-74,信号未确认 → 观察池。等待资金/估值标签改善。',
    REDUCE: '短线急跌 -4%+,暂时止损出场,避免深套。',
    AVOID: '风险>60 或总分不及格 → 「不参与」,等下次窗口。',
    EXIT: 'PE≥100 + 风险>25 → 「建议清仓」估值严重高估,即使其它维度亮眼也要离场。'
  };

  // 估值带 → 解释
  const BAND_DESC = {
    '深度低估': 'PE<15 或 PB≤1 → 深度估值底,价值投资者重点关注。',
    '低估': 'PE 15-25 或 PB 1-1.5 → 低估区间,基本面好可介入。',
    '合理': 'PE 25-40 → 估值公允,看成长性是否匹配。',
    '偏高': 'PE 40-80 或 PB 3-6 → 估值略贵,需要其它维度补偿。',
    '高估': 'PE>80 或 PB>6 → 估值高位,谨慎介入或考虑减仓。',
    '亏损': 'PE≤0,公司当前亏损,估值法失效,看基本面的拐点信号。',
    '增速缺失': '净利润同比≤0,PEG 不可计算 → 看趋势是否反转。',
    '极低估': 'PEG<0.5 → 增速远高于估值,「黄金坑」窗口。',
    '优秀': 'OCF/净利润 ≥ 1.0 → 盈利质量好,账面盈利是真金白银。',
    '健康': 'OCF/净利润 0.5-1.0 → 经营现金流覆盖利润。',
    '偏弱': 'OCF/净利润 0-0.5 → 现金流偏紧,要观察是否恶化。',
    '恶化': 'OCF/净利润 < 0 → 账面盈利但现金流失,警惕。'
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
    const tag = U.el('span', peBandClass(band), band);
    // 给估值带加 tooltip:鼠标悬停解释含义（移动端长按可见）
    const desc = BAND_DESC[band];
    if (desc) tag.title = band + '\n' + desc;
    return tag;
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

    // 相对板块强度 + 20 日价格位置
    const tdRel = U.el('td', 'val-rel');
    if (U.isNum(s.relative_chg)) {
      // A 股惯例：跑赢板块（正超额）用红色、跑输用绿色
      const cls = s.relative_chg >= 0 ? 'up' : 'down';
      tdRel.appendChild(U.el('div', 'val-num ' + cls,
        (s.relative_chg >= 0 ? '+' : '') + s.relative_chg.toFixed(2) + '%'));
      tdRel.appendChild(U.el('div', 'val-meta', 'vs 板块'));
    } else {
      tdRel.appendChild(U.el('div', 'val-meta', U.NBSP));
    }
    if (U.isNum(s.position_pct)) {
      // 位置越低越安全（回踩充分），70% 以上标警戒色提示追高
      const posCls = s.position_pct >= 75 ? 'val-pos-high' : (s.position_pct <= 30 ? 'val-pos-low' : '');
      tdRel.appendChild(U.el('div', 'val-meta ' + posCls,
        '位置 ' + Math.round(s.position_pct) + '%'));
    }
    tr.appendChild(tdRel);

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
     '相对板块 · 位置', '综合分 · 分级', '建议 · 风险点'].forEach(function (h) {
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
    const vTitle = U.el('h2', 'val-title');
    vTitle.appendChild(U.icon('diamond', { size: 16 }));
    vTitle.appendChild(document.createTextNode(' 价值投资'));
    head.appendChild(vTitle);
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
    // 权重调节按钮：折叠面板，展开 5 个维度滑块
    const weightsBtn = U.el('button', 'btn btn-sm');
    weightsBtn.appendChild(U.icon('settings', { size: 14 }));
    weightsBtn.appendChild(document.createTextNode(' 权重'));
    weightsBtn.title = '调整基本面/板块/资金/量价/情绪 5 维度的相对权重';
    weightsBtn.onclick = function () {
      const panel = document.getElementById('val-weights-panel');
      if (!panel) return;
      const hidden = panel.style.display === 'none';
      panel.style.display = hidden ? '' : 'none';
      weightsBtn.textContent = '';
      weightsBtn.appendChild(U.icon('settings', { size: 14 }));
      weightsBtn.appendChild(document.createTextNode(hidden ? ' 权重 ▲' : ' 权重 ▼'));
    };
    right.appendChild(weightsBtn);

    right.appendChild(refresh);
    head.appendChild(right);
    root.appendChild(head);

    // 权重面板（默认隐藏，异步加载当前权重）
    const weightsPanel = U.el('div', 'val-weights-panel', '');
    weightsPanel.id = 'val-weights-panel';
    weightsPanel.style.display = 'none';
    weightsPanel.appendChild(U.el('div', 'val-weights-title',
      '各维度权重（0.2 弱 — 1.0 平衡 — 3.0 强；保存后立即对候选池重新排序）'));
    const sliders = U.el('div', 'val-weights-sliders');
    weightsPanel.appendChild(sliders);
    const actions = U.el('div', 'val-weights-actions');
    const saveBtn = U.el('button', 'btn btn-sm btn-primary', '保存权重');
    const resetBtn = U.el('button', 'btn btn-sm', '恢复默认');
    const status = U.el('span', 'val-weights-status');
    actions.appendChild(saveBtn); actions.appendChild(resetBtn); actions.appendChild(status);
    weightsPanel.appendChild(actions);
    root.appendChild(weightsPanel);
    state._weightsUI = { sliders: sliders, saveBtn: saveBtn, resetBtn: resetBtn, status: status };

    viewEl.appendChild(root);

    // 异步加载当前权重（填充滑块初值）
    loadWeights();
  }

  const WEIGHT_FIELDS = [
    { key: 'finance', label: '基本面', desc: '成长 + 质量 + 估值 + 现金流 + 行业（共 50 分）' },
    { key: 'board',   label: '板块',   desc: '候选所属板块的涨停数与平均涨幅' },
    { key: 'flow',    label: '资金',   desc: '近 5/30 日主力资金方向与拐点' },
    { key: 'volume',  label: '量价',   desc: '量比、换手、涨跌幅' },
    { key: 'emotion', label: '情绪',   desc: '连板梯队与高换手活跃度' },
    { key: 'relative', label: '相对强度', desc: '个股涨幅 − 所属板块平均涨幅（强者恒强）' },
    { key: 'position', label: '价格位置', desc: '当前价在近 20 日高低区间的百分位（低位加分）' }
  ];

  async function loadWeights() {
    if (!state._weightsUI) return;
    try {
      const cfg = await API.valueWeights();
      const sliders = state._weightsUI.sliders;
      sliders.innerHTML = '';
      const inputs = {};
      WEIGHT_FIELDS.forEach(function (f) {
        const wrap = U.el('div', 'val-weight');
        wrap.appendChild(U.el('div', 'val-weight-label', f.label));
        wrap.appendChild(U.el('div', 'val-weight-desc', f.desc));
        const row = U.el('div', 'val-weight-row');
        const slider = U.el('input', 'val-weight-slider');
        slider.type = 'range';
        slider.min = (cfg.range && cfg.range[0]) || 0.2;
        slider.max = (cfg.range && cfg.range[1]) || 3.0;
        slider.step = 0.1;
        slider.value = (cfg[f.key] != null) ? cfg[f.key] : 1.0;
        const num = U.el('input', 'val-weight-num');
        num.type = 'number';
        num.min = slider.min; num.max = slider.max; num.step = 0.1;
        num.value = slider.value;
        // 联动：拖动 / 输入同步两份
        slider.oninput = function () { num.value = slider.value; };
        num.oninput = function () {
          const v = Math.max(parseFloat(slider.min), Math.min(parseFloat(slider.max), parseFloat(num.value) || 1.0));
          slider.value = v;
        };
        inputs[f.key] = { slider: slider, num: num };
        row.appendChild(slider); row.appendChild(num);
        wrap.appendChild(row);
        sliders.appendChild(wrap);
      });
      state._weightsUI.inputs = inputs;
      // 暴露基线权重（用于基准提示）
      state._weightsUI.baseTotal = cfg.base_total || 92;
    } catch (e) {
      // 静默失败：滑块用默认值 1.0 即可
    }

    const saveBtn = state._weightsUI.saveBtn;
    const resetBtn = state._weightsUI.resetBtn;
    saveBtn.onclick = async function () {
      const inputs = state._weightsUI.inputs || {};
      const body = {};
      Object.keys(inputs).forEach(function (k) {
        body[k] = parseFloat(inputs[k].slider.value) || 1.0;
      });
      saveBtn.disabled = true;
      resetBtn.disabled = true;
      state._weightsUI.status.textContent = '保存中…';
      try {
        await API.valueWeightsSave(body);
        state._weightsUI.status.textContent = '✓ 已保存';
        // 权重改了 → 缓存指纹变了,30s 后再拉一次
        setTimeout(function () {
          state._weightsUI.status.textContent = '';
        }, 4000);
      } catch (e) {
        state._weightsUI.status.textContent = '✗ ' + (e.message || '保存失败');
      } finally {
        saveBtn.disabled = false;
        resetBtn.disabled = false;
      }
    };
    resetBtn.onclick = async function () {
      saveBtn.disabled = true;
      resetBtn.disabled = true;
      state._weightsUI.status.textContent = '恢复中…';
      try {
        const cfg = await API.valueWeightsReset();
        // 用后端返回的实际值重置滑块
        const inputs = state._weightsUI.inputs || {};
        Object.keys(inputs).forEach(function (k) {
          if (cfg[k] != null) {
            inputs[k].slider.value = cfg[k];
            inputs[k].num.value = cfg[k];
          }
        });
        state._weightsUI.status.textContent = '✓ 已恢复默认';
        setTimeout(function () { state._weightsUI.status.textContent = ''; }, 4000);
      } catch (e) {
        state._weightsUI.status.textContent = '✗ ' + (e.message || '重置失败');
      } finally {
        saveBtn.disabled = false;
        resetBtn.disabled = false;
      }
    };
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
    const titleMap = { core: '核心机会池', trend: '趋势池', emotion: '情绪妖股池' };
    const totalStocks = Object.keys(pools).reduce(function (acc, k) {
      return acc + ((pools[k] || []).length);
    }, 0);

    Object.keys(pools).forEach(function (k) {
      const poolEl = renderPool(titleMap[k] || k, POOL_DESC[k], pools[k] || []);
      root.appendChild(poolEl);
    });

    // 没有候选时的空状态卡（接口正常但市场弱势 / 时段等）
    if (totalStocks === 0) {
      root.appendChild(renderEmpty(data));
    }

    // 风险提示（市场本身的状态）
    if (data.market && data.market.tip) {
      root.appendChild(U.el('div', 'val-market-tip', data.market.tip));
    }
  }

  function renderEmpty(data) {
    const mkt = data.market || {};
    const wrap = U.el('div', 'val-empty-card');
    const vEmpty = U.el('div', 'val-empty-icon');
    vEmpty.appendChild(U.icon('inbox', { size: 36 }));
    wrap.appendChild(vEmpty);
    const head = U.el('div', 'val-empty-title', '当前市场状态下暂无符合条件的候选');
    wrap.appendChild(head);

    // 给出明确"为什么是空"的可读解释
    const reasons = [];
    if (typeof mkt.zt_count === 'number' && mkt.zt_count <= 0) {
      reasons.push('当前交易日涨停数为 0（无涨停池 → 无候选）');
    }
    if (mkt.state === 'F' || mkt.state === 'E') {
      reasons.push('市场状态判定为 ' + (mkt.name || mkt.state) + '（退潮）');
    } else if (mkt.state === 'D') {
      reasons.push('市场状态判定为 ' + (mkt.name || mkt.state) + '（震荡存量）');
    }
    if (typeof mkt.candidate_count === 'number' && mkt.candidate_count === 0) {
      reasons.push('初始候选池即为 0（已剔除 ETF / 北交所 / 非 A 股代码）');
    }
    if (reasons.length === 0) {
      reasons.push('暂未发现基本合格 + 风险可控的 A 股标的');
    }
    const list = U.el('ul', 'val-empty-reasons');
    reasons.forEach(function (r) { list.appendChild(U.el('li', '', r)); });
    wrap.appendChild(list);

    const tips = U.el('div', 'val-empty-tips');
    tips.appendChild(U.el('div', 'val-empty-tip-title', '建议'));
    tips.appendChild(U.el('div', '', '① 开盘 9:30~10:30 期间再来（候选池会更活跃）'));
    const tip2 = U.el('div', '');
    tip2.appendChild(document.createTextNode('② 降低对资金/情绪维度的权重，单纯看估值（点 '));
    tip2.appendChild(U.icon('settings', { size: 12 }));
    tip2.appendChild(document.createTextNode(' 权重调整）'));
    tips.appendChild(tip2);
    tips.appendChild(U.el('div', '', '③ 或点击右上「刷新数据」重算候选'));
    wrap.appendChild(tips);

    if (data.generated_at) {
      wrap.appendChild(U.el('div', 'val-empty-time', '生成于 ' + data.generated_at));
    }
    return wrap;
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

  export const PageValue = {
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
