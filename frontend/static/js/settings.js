/* AI 模型配置弹窗（顶栏 ⚙ 入口）：支持多套模型档案 + 自动故障转移 */
(function (global) {
  'use strict';

  const state = {
    vendors: [],
    profiles: [],     // 当前编辑中的档案列表
    weights: null,
    nextIdx: 0         // 新档案序号
  };
  // 每张卡片私有的自绘模型下拉菜单状态（key = 卡片索引）
  const menus = {};

  function root() { return document.getElementById('modal-settings'); }
  function profilesBox() { return document.getElementById('cfg-profiles'); }
  function note() { return document.getElementById('cfg-note'); }

  function setNote(text, kind) {
    note().textContent = text;
    note().className = 'cfg-note' + (kind ? ' ' + kind : '');
  }

  // ------------------------------------------------------------------ 评分权重（保持不变）
  function weightInputs() {
    return {
      tech: document.getElementById('w-tech'),
      capital: document.getElementById('w-capital'),
      news: document.getElementById('w-news')
    };
  }
  function weightResult() { return document.getElementById('w-result'); }

  function fillWeights(w) {
    const inputs = weightInputs();
    inputs.tech.value = w.tech;
    inputs.capital.value = w.capital;
    inputs.news.value = w.news;
    weightResult().textContent = w.source === 'env' ? '（环境变量）' : '（界面配置）';
  }

  async function saveWeights() {
    const inputs = weightInputs();
    try {
      const res = await API.scoreWeightsSave({
        tech: inputs.tech.value,
        capital: inputs.capital.value,
        news: inputs.news.value
      });
      state.weights = res;
      fillWeights(res);
      weightResult().textContent = '已保存 ✓（AI 缓存已失效）';
      weightResult().className = 'cfg-test-result ok';
      U.toast('评分权重已保存，AI 分析缓存已刷新', 'ok');
    } catch (err) {
      U.toast('保存权重失败：' + err.message, 'err');
    }
  }

  async function resetWeights() {
    if (!confirm('恢复评分权重为默认（环境变量）？')) return;
    try {
      const res = await API.scoreWeightsReset();
      state.weights = res;
      fillWeights(res);
      weightResult().textContent = '已恢复默认 ✓';
      weightResult().className = 'cfg-test-result ok';
      U.toast('评分权重已恢复默认', 'ok');
    } catch (err) {
      U.toast('恢复权重失败：' + err.message, 'err');
    }
  }

  // ------------------------------------------------------------------ 档案卡片渲染
  function makeId() { return 'p' + Date.now().toString(36) + (state.nextIdx++).toString(36); }

  function cardFields(idx) {
    const card = profilesBox().querySelectorAll('.cfg-profile')[idx];
    if (!card) return null;
    return {
      card: card,
      name: card.querySelector('.cfg-p-name'),
      vendor: card.querySelector('.cfg-p-vendor'),
      baseUrl: card.querySelector('.cfg-p-base-url'),
      model: card.querySelector('.cfg-p-model'),
      apiKey: card.querySelector('.cfg-p-api-key'),
      enabled: card.querySelector('.cfg-p-enabled'),
      jsonMode: card.querySelector('.cfg-p-json-mode'),
      primary: card.querySelector('.cfg-p-primary'),
      result: card.querySelector('.cfg-test-result'),
      menu: card.querySelector('.cfg-model-menu'),
      modelsBtn: card.querySelector('.cfg-p-models')
    };
  }

  function applyPreset(idx) {
    const f = cardFields(idx);
    const v = state.vendors.find(function (x) { return x.id === f.vendor.value; });
    if (!v) return;
    if (v.base_url) f.baseUrl.value = v.base_url;
    if (v.model) f.model.value = v.model;
    f.jsonMode.checked = !!v.json_mode;
  }

  function profileFromCard(idx) {
    const f = cardFields(idx);
    return {
      id: state.profiles[idx].id,
      name: f.name.value.trim(),
      enabled: f.enabled.checked,
      primary: f.primary.checked,
      vendor: f.vendor.value,
      base_url: f.baseUrl.value.trim(),
      model: f.model.value.trim(),
      api_key: f.apiKey.value,
      json_mode: f.jsonMode.checked
    };
  }

  function renderCard(profile, idx) {
    const card = U.el('div', 'cfg-profile');
    card.dataset.idx = String(idx);

    const head = U.el('div', 'cfg-p-head');
    const nameWrap = U.el('span', 'cfg-p-name-wrap');
    const nameInput = U.el('input', 'cfg-p-name');
    nameInput.type = 'text';
    nameInput.spellcheck = false;
    nameInput.placeholder = '模型名称（如 DeepSeek 主模型）';
    nameInput.value = profile.name || '';
    nameWrap.appendChild(nameInput);

    const primaryLabel = U.el('label', 'cfg-p-primary-label');
    const primaryBox = U.el('input', 'cfg-p-primary');
    primaryBox.type = 'checkbox';
    primaryBox.checked = !!profile.primary;
    primaryBox.title = '标记为主模型：调用时优先，失败自动切换下一个';
    primaryLabel.appendChild(primaryBox);
    primaryLabel.appendChild(document.createTextNode('主模型'));
    nameWrap.appendChild(primaryLabel);

    const del = U.el('button', 'btn btn-sm btn-ghost cfg-p-del', '删除');
    del.type = 'button';
    del.title = '删除该模型档案';
    head.appendChild(nameWrap);
    head.appendChild(del);

    // 厂商预设
    const rowVendor = U.el('div', 'cfg-row');
    const lblVendor = U.el('label', '', '厂商预设');
    lblVendor.htmlFor = 'cfg-vendor-' + idx;
    const selVendor = U.el('select', 'cfg-p-vendor');
    selVendor.id = 'cfg-vendor-' + idx;
    state.vendors.forEach(function (v) {
      const opt = document.createElement('option');
      opt.value = v.id;
      opt.textContent = v.label;
      selVendor.appendChild(opt);
    });
    selVendor.value = profile.vendor || 'custom';
    rowVendor.appendChild(lblVendor);
    rowVendor.appendChild(selVendor);

    // Base URL
    const rowUrl = U.el('div', 'cfg-row');
    const lblUrl = U.el('label', '', 'Base URL');
    lblUrl.htmlFor = 'cfg-base-url-' + idx;
    const inputUrl = U.el('input', 'cfg-p-base-url');
    inputUrl.id = 'cfg-base-url-' + idx;
    inputUrl.type = 'text';
    inputUrl.spellcheck = false;
    inputUrl.placeholder = 'https://api.deepseek.com/v1';
    inputUrl.value = profile.base_url || '';
    rowUrl.appendChild(lblUrl);
    rowUrl.appendChild(inputUrl);

    // 模型名称 + 获取模型
    const rowModel = U.el('div', 'cfg-row');
    const lblModel = U.el('label', '', '模型名称');
    lblModel.htmlFor = 'cfg-model-' + idx;
    const wrap = U.el('div', 'cfg-model-wrap');
    const field = U.el('div', 'cfg-model-field');
    const inputModel = U.el('input', 'cfg-p-model');
    inputModel.id = 'cfg-model-' + idx;
    inputModel.type = 'text';
    inputModel.spellcheck = false;
    inputModel.autocomplete = 'off';
    inputModel.placeholder = 'deepseek-chat';
    inputModel.value = profile.model || '';
    const menu = U.el('div', 'cfg-model-menu');
    menu.hidden = true;
    field.appendChild(inputModel);
    field.appendChild(menu);
    const btnModels = U.el('button', 'btn btn-sm cfg-p-models', '获取模型');
    btnModels.type = 'button';
    btnModels.title = '从云端拉取该服务支持的模型列表';
    wrap.appendChild(field);
    wrap.appendChild(btnModels);
    rowModel.appendChild(lblModel);
    rowModel.appendChild(wrap);

    // API Key
    const rowKey = U.el('div', 'cfg-row');
    const lblKey = U.el('label', '', 'API Key');
    lblKey.htmlFor = 'cfg-api-key-' + idx;
    const inputKey = U.el('input', 'cfg-p-api-key');
    inputKey.id = 'cfg-api-key-' + idx;
    inputKey.type = 'password';
    inputKey.autocomplete = 'off';
    inputKey.placeholder = profile.api_key_set ? '已保存密钥会保留，留空不变' : '请输入 API Key';
    rowKey.appendChild(lblKey);
    rowKey.appendChild(inputKey);

    // 启用 / JSON 模式
    const rowChecks = U.el('div', 'cfg-row cfg-check');
    const lblEnabled = U.el('label', '', '启用该模型');
    const chkEnabled = U.el('input', 'cfg-p-enabled');
    chkEnabled.type = 'checkbox';
    chkEnabled.checked = !!profile.enabled;
    rowChecks.appendChild(lblEnabled);
    rowChecks.appendChild(chkEnabled);

    const rowJson = U.el('div', 'cfg-row cfg-check');
    const lblJson = U.el('label', '', 'JSON 模式（不兼容的端点请关闭）');
    const chkJson = U.el('input', 'cfg-p-json-mode');
    chkJson.type = 'checkbox';
    chkJson.checked = !!profile.json_mode;
    rowJson.appendChild(lblJson);
    rowJson.appendChild(chkJson);

    // 测试连接
    const actions = U.el('div', 'cfg-actions');
    const btnTest = U.el('button', 'btn cfg-p-test', '测试连接');
    btnTest.type = 'button';
    const result = U.el('span', 'cfg-test-result');
    actions.appendChild(btnTest);
    actions.appendChild(result);

    card.appendChild(head);
    card.appendChild(rowVendor);
    card.appendChild(rowUrl);
    card.appendChild(rowModel);
    card.appendChild(rowKey);
    card.appendChild(rowChecks);
    card.appendChild(rowJson);
    card.appendChild(actions);
    return card;
  }

  function bindCard(idx) {
    const f = cardFields(idx);
    f.vendor.addEventListener('change', function () { applyPreset(idx); });
    f.primary.addEventListener('change', function () {
      // 主模型唯一：勾选本卡时取消其他卡，并同步回 state（重渲染不丢失）
      state.profiles.forEach(function (p, i) {
        p.primary = (i === idx) && f.primary.checked;
        if (i !== idx) cardFields(i).primary.checked = p.primary;
      });
    });
    f.del = f.card.querySelector('.cfg-p-del');
    f.del.addEventListener('click', function () {
      if (!confirm('删除该模型档案？')) return;
      state.profiles.splice(idx, 1);
      renderAll();
    });
    f.test = f.card.querySelector('.cfg-p-test');
    f.test.addEventListener('click', function () { testConnection(idx); });
    f.modelsBtn.addEventListener('click', function () { fetchModels(idx); });

    // 自绘模型下拉菜单
    f.model.addEventListener('focus', function () { openMenu(idx); });
    f.model.addEventListener('input', function () { openMenu(idx); });
    f.model.addEventListener('keydown', function (e) {
      const menuState = menus[idx] || { active: -1 };
      const open = !f.menu.hidden;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (!open) openMenu(idx); else moveActive(idx, 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (open) moveActive(idx, -1);
      } else if (e.key === 'Enter') {
        if (open && menuState.active >= 0) {
          const items = f.menu.querySelectorAll('.cfg-model-item');
          if (items[menuState.active]) {
            e.preventDefault();
            selectModel(idx, items[menuState.active].textContent);
          }
        }
      } else if (e.key === 'Escape') {
        if (open) { e.stopPropagation(); closeMenu(idx); }
      }
    });
  }

  function renderAll() {
    profilesBox().innerHTML = '';
    state.profiles.forEach(function (p, i) {
      profilesBox().appendChild(renderCard(p, i));
      bindCard(i);
    });
  }

  // ---- 自绘模型下拉菜单（每卡独立）----
  function menuModels(idx) {
    const f = cardFields(idx);
    const kw = (f.model.value || '').trim().toLowerCase();
    const all = menus[idx] && menus[idx].models ? menus[idx].models : [];
    if (!kw) return all.slice();
    return all.filter(function (m) { return m.toLowerCase().indexOf(kw) >= 0; });
  }

  function renderMenu(idx) {
    const f = cardFields(idx);
    f.menu.innerHTML = '';
    const items = menuModels(idx);
    if (!items.length) {
      f.menu.appendChild(U.el('div', 'cfg-model-empty', '无匹配模型，可直接输入自定义模型名'));
      return items;
    }
    items.forEach(function (name) {
      const it = U.el('div', 'cfg-model-item', name);
      it.addEventListener('mousedown', function (e) { e.preventDefault(); });
      it.addEventListener('click', function () { selectModel(idx, name); });
      f.menu.appendChild(it);
    });
    return items;
  }

  function openMenu(idx) {
    const f = cardFields(idx);
    if (!menus[idx]) menus[idx] = { models: [], active: -1 };
    const items = renderMenu(idx);
    menus[idx].active = items.length ? 0 : -1;
    highlightMenu(idx);
    f.menu.hidden = false;
  }

  function closeMenu(idx) {
    const f = cardFields(idx);
    if (!f || !f.menu) return;
    f.menu.hidden = true;
    if (menus[idx]) menus[idx].active = -1;
  }

  function highlightMenu(idx) {
    const f = cardFields(idx);
    const items = f.menu.querySelectorAll('.cfg-model-item');
    const active = menus[idx] ? menus[idx].active : -1;
    items.forEach(function (el, i) {
      el.classList.toggle('active', i === active);
    });
  }

  function moveActive(idx, step) {
    const f = cardFields(idx);
    const items = f.menu.querySelectorAll('.cfg-model-item');
    if (!items.length) return;
    const m = menus[idx] || { active: -1, models: [] };
    m.active = (m.active + step + items.length) % items.length;
    highlightMenu(idx);
    const el = items[m.active];
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
  }

  function selectModel(idx, name) {
    const f = cardFields(idx);
    f.model.value = name;
    closeMenu(idx);
  }

  // ------------------------------------------------------------------ 档案操作
  async function fetchModels(idx) {
    const f = cardFields(idx);
    const btn = f.modelsBtn;
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = '获取中…';
    try {
      const res = await API.llmModels(profileFromCard(idx));
      if (res.ok && res.models && res.models.length) {
        if (!menus[idx]) menus[idx] = { models: [], active: -1 };
        menus[idx].models = res.models.slice();
        setNote('已获取 ' + res.models.length + ' 个模型，点模型输入框可下拉选择。', 'info');
        openMenu(idx);
      } else {
        setNote(res.message || '获取失败', 'err');
      }
    } catch (err) {
      setNote('获取失败：' + err.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  }

  async function testConnection(idx) {
    const f = cardFields(idx);
    f.result.textContent = '测试中…';
    f.result.className = 'cfg-test-result';
    try {
      const res = await API.llmTest(profileFromCard(idx));
      f.result.textContent = res.message || '';
      f.result.className = 'cfg-test-result ' + (res.ok ? 'ok' : 'err');
    } catch (err) {
      f.result.textContent = '测试失败：' + err.message;
      f.result.className = 'cfg-test-result err';
    }
  }

  async function save() {
    try {
      const profiles = state.profiles.map(function (_, i) { return profileFromCard(i); });
      if (!profiles.length) {
        U.toast('请至少保留一个模型档案', 'err');
        return;
      }
      if (!profiles.some(function (p) { return p.primary; })) {
        const first = profiles.find(function (p) { return p.enabled; }) || profiles[0];
        if (first) first.primary = true;
      }
      const res = await API.llmSave({ profiles: profiles });
      state.profiles = res.profiles || [];
      renderAll();
      U.toast(res.engine === 'llm' ? '配置已保存，AI 分析已切换到大模型' : '配置已保存', 'ok');
      if (global.App && App.refreshMeta) App.refreshMeta();
      close();
    } catch (err) {
      U.toast('保存失败：' + err.message, 'err');
    }
  }

  async function reset() {
    if (!confirm('清除界面保存的所有 AI 模型档案，回退到环境变量设置？')) return;
    try {
      await API.llmReset();
      const cfg = await API.llmConfig();
      state.vendors = cfg.vendors || [];
      state.profiles = cfg.profiles || [];
      renderAll();
      U.toast('已恢复默认（环境变量）', 'ok');
      if (global.App && App.refreshMeta) App.refreshMeta();
    } catch (err) {
      U.toast('恢复失败：' + err.message, 'err');
    }
  }

  async function open() {
    try {
      const cfg = await API.llmConfig();
      state.vendors = cfg.vendors || [];
      state.profiles = cfg.profiles || [];
      state.nextIdx = 0;
      renderAll();
      setNote('');
      root().hidden = false;
      document.body.style.overflow = 'hidden';
      try {
        const w = await API.scoreWeights();
        state.weights = w;
        fillWeights(w);
      } catch (e) { /* 权重加载失败不阻塞设置弹窗 */ }
    } catch (err) {
      U.toast('读取配置失败：' + err.message, 'err');
    }
  }

  function close() {
    root().hidden = true;
    document.body.style.overflow = '';
  }

  function bind() {
    document.getElementById('cfg-add').addEventListener('click', function () {
      state.profiles.push({
        id: makeId(),
        name: '模型 ' + (state.profiles.length + 1),
        enabled: true,
        primary: state.profiles.length === 0,
        vendor: 'custom',
        base_url: '',
        model: '',
        api_key_set: false,
        json_mode: true
      });
      renderAll();
    });
    document.getElementById('cfg-save').addEventListener('click', save);
    document.getElementById('cfg-reset').addEventListener('click', reset);
    document.getElementById('w-save').addEventListener('click', saveWeights);
    document.getElementById('w-reset').addEventListener('click', resetWeights);
    // 点击模型菜单外关闭（只绑定一次，避免重渲染重复累积）
    document.addEventListener('mousedown', function (e) {
      const wrap = e.target.closest ? e.target.closest('.cfg-model-wrap') : null;
      if (!wrap) {
        Object.keys(menus).forEach(function (k) { closeMenu(Number(k)); });
      }
    });

    root().addEventListener('click', function (e) {
      if (e.target.getAttribute('data-close-settings') === '1') close();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !root().hidden) close();
    });
  }

  global.Settings = {
    open: open,
    close: close,
    bind: bind
  };

  document.addEventListener('DOMContentLoaded', function () { Settings.bind(); });
})(window);
