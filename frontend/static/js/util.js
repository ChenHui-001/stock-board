/* 通用工具函数：格式化、涨跌配色、DOM 助手、提示 */
(function (global) {
  'use strict';

  const NBSP = '--';

  function isNum(v) {
    return typeof v === 'number' && isFinite(v);
  }

  /** 价格：固定 2 位小数（需求 2.2） */
  function price(v) {
    return isNum(v) ? v.toFixed(2) : NBSP;
  }

  /** 涨跌幅：2 位小数 + 正号 + % */
  function pct(v) {
    if (!isNum(v)) return NBSP;
    return (v > 0 ? '+' : '') + v.toFixed(2) + '%';
  }

  /** 金额自适应单位 */
  function money(v, digits) {
    if (!isNum(v)) return NBSP;
    const d = digits == null ? 2 : digits;
    const abs = Math.abs(v);
    if (abs >= 1e8) return (v / 1e8).toFixed(d) + '亿';
    if (abs >= 1e4) return (v / 1e4).toFixed(d) + '万';
    return v.toFixed(0);
  }

  /** 带正负号的金额（资金流向用） */
  function signedMoney(v, digits) {
    if (!isNum(v)) return NBSP;
    return (v > 0 ? '+' : '') + money(v, digits);
  }

  /** 成交量：股 -> 万手/手 */
  function volume(v) {
    if (!isNum(v)) return NBSP;
    const hands = v / 100;
    if (hands >= 1e4) return (hands / 1e4).toFixed(2) + '万手';
    return hands.toFixed(0) + '手';
  }

  /** 量比：2 位小数，无符号 */
  function ratio(v) {
    return isNum(v) ? v.toFixed(2) : NBSP;
  }

  /** 换手率：2 位小数 + %，不带正号 */
  function turnover(v) {
    return isNum(v) ? v.toFixed(2) + '%' : NBSP;
  }

  /** 涨跌配色：正红 / 负绿 / 零灰（需求 2.2） */
  function tone(v) {
    if (!isNum(v) || v === 0) return 'flat';
    return v > 0 ? 'up' : 'down';
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function debounce(fn, wait) {
    let timer = null;
    return function () {
      const args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait);
    };
  }

  let toastTimer = [];
  function toast(message, kind) {
    const root = document.getElementById('toast-root');
    if (!root) return;
    const node = el('div', 'toast' + (kind ? ' ' + kind : ''), message);
    root.appendChild(node);
    const t = setTimeout(function () {
      node.style.opacity = '0';
      node.style.transition = 'opacity .25s';
      setTimeout(function () { node.remove(); }, 250);
    }, 2600);
    toastTimer.push(t);
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) { /* 回退到 execCommand */ }
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch (e) {
      return false;
    }
  }

  global.U = {
    NBSP: NBSP,
    isNum: isNum,
    price: price,
    pct: pct,
    money: money,
    signedMoney: signedMoney,
    volume: volume,
    ratio: ratio,
    turnover: turnover,
    tone: tone,
    el: el,
    escapeHtml: escapeHtml,
    debounce: debounce,
    toast: toast,
    copyText: copyText
  };
})(window);
