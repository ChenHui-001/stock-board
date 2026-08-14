/* AI 模型配置弹窗（顶栏 ⚙ 入口） */
(function (global) {
  'use strict';

  const state = { vendors: [], config: null, testing: false };

  function root() { return document.getElementById('modal-settings'); }
  function vendorSel() { return document.getElementById('cfg-vendor'); }
  function baseUrl() { return document.getElementById('cfg-base-url'); }
  function model() { return document.getElementById('cfg-model'); }
  function apiKey() { return document.getElementById('cfg-api-key'); }
  function enabledBox() { return document.getElementById('cfg-enabled'); }
  function jsonBox() { return document.getElementById('cfg-json-mode'); }
  function note() { return document.getElementById('cfg-note'); }
  function testResult() { return document.getElementById('cfg-test-result'); }

  function setNote(html, kind) {
    note().innerHTML = html;
    note().className = 'cfg-note' + (kind ? ' ' + kind : '');
  }

  function fillVendorSelect() {
    vendorSel().innerHTML = '';
    state.vendors.forEach(function (v) {
      const opt = document.createElement('option');
      opt.value = v.id;
      opt.textContent = v.label;
      vendorSel().appendChild(opt);
    });
  }

  function applyPreset(id) {
    const v = state.vendors.find(function (x) { return x.id === id; });
    if (!v) return;
    if (v.base_url) baseUrl().value = v.base_url;
    if (v.model) model().value = v.model;
    jsonBox().checked = !!v.json_mode;
  }

  function formConfig() {
    return {
      enabled: enabledBox().checked,
      vendor: vendorSel().value,
      base_url: baseUrl().value.trim(),
      model: model().value.trim(),
      api_key: apiKey().value,
      json_mode: jsonBox().checked
    };
  }

  function fillForm(cfg) {
    enabledBox().checked = !!cfg.enabled;
    vendorSel().value = cfg.vendor || 'custom';
    baseUrl().value = cfg.base_url || '';
    model().value = cfg.model || '';
    apiKey().value = '';
    jsonBox().checked = !!cfg.json_mode;
    setNote(cfg.api_key_set
      ? '已保存 API Key（界面不回显，留空保存即保持不变）'
      : '当前未配置 API Key，AI 分析将使用内置规则引擎。', 'info');
  }

  async function open() {
    try {
      const cfg = await API.llmConfig();
      state.vendors = cfg.vendors || [];
      state.config = cfg;
      fillVendorSelect();
      fillForm(cfg);
      testResult().textContent = '';
      root().hidden = false;
      document.body.style.overflow = 'hidden';
    } catch (err) {
      U.toast('读取配置失败：' + err.message, 'err');
    }
  }

  function close() {
    root().hidden = true;
    document.body.style.overflow = '';
  }

  function modelList() { return document.getElementById('cfg-model-list'); }
  function modelsBtn() { return document.getElementById('cfg-models'); }

  async function fetchModels(silent) {
    const btn = modelsBtn();
    const dl = modelList();
    dl.innerHTML = '';
    if (!btn.disabled && !silent) {
      btn.disabled = true;
      btn.textContent = '获取中…';
    }
    try {
      const res = await API.llmModels(formConfig());
      if (res.ok && res.models && res.models.length) {
        res.models.forEach(function (m) {
          const opt = document.createElement('option');
          opt.value = m;
          dl.appendChild(opt);
        });
        if (!silent) setNote('已获取 ' + res.models.length + ' 个模型，点输入框可下拉选择。', 'info');
      } else if (!silent) {
        setNote(res.message || '获取失败', 'err');
      }
    } catch (err) {
      if (!silent) setNote('获取失败：' + err.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '获取模型';
    }
  }

  async function testConnection() {
    if (state.testing) return;
    state.testing = true;
    testResult().textContent = '测试中…';
    try {
      const res = await API.llmTest(formConfig());
      testResult().textContent = res.message || '';
      testResult().className = 'cfg-test-result ' + (res.ok ? 'ok' : 'err');
    } catch (err) {
      testResult().textContent = '测试失败：' + err.message;
      testResult().className = 'cfg-test-result err';
    } finally {
      state.testing = false;
    }
  }

  async function save() {
    try {
      const cfg = formConfig();
      const res = await API.llmSave(cfg);
      state.config = res;
      fillForm(res);
      U.toast(res.api_key_set ? '配置已保存，AI 分析已切换到大模型' : '配置已保存', 'ok');
      if (global.App && App.refreshMeta) App.refreshMeta();
      close();
    } catch (err) {
      U.toast('保存失败：' + err.message, 'err');
    }
  }

  async function reset() {
    if (!confirm('清除界面保存的 AI 配置，回退到环境变量设置？')) return;
    try {
      await API.llmReset();
      const cfg = await API.llmConfig();
      state.config = cfg;
      state.vendors = cfg.vendors || [];
      fillVendorSelect();
      fillForm(cfg);
      U.toast('已恢复默认（环境变量）', 'ok');
      if (global.App && App.refreshMeta) App.refreshMeta();
    } catch (err) {
      U.toast('恢复失败：' + err.message, 'err');
    }
  }

  function bind() {
    vendorSel().addEventListener('change', function () {
      applyPreset(vendorSel().value);
      setNote('');
      // 已保存过密钥时，切换厂商自动拉取该服务的模型列表
      if (state.config && state.config.api_key_set) fetchModels(true);
    });
    document.getElementById('cfg-models').addEventListener('click', function () {
      fetchModels(false);
    });
    document.getElementById('cfg-test').addEventListener('click', testConnection);
    document.getElementById('cfg-save').addEventListener('click', save);
    document.getElementById('cfg-reset').addEventListener('click', reset);
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
