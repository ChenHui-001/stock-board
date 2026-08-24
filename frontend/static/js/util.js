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

  /**
   * 外链地址白名单：只放行 http/https，其余一律返回 ''。
   *
   * 资讯/快讯的 url 来自第三方源（东财、新浪、同花顺 7x24 等），后端只做 strip
   * 不校验协议。若某条 feed 携带 javascript: 或 data: 地址，直接赋给 a.href 会在
   * 用户点击标题时于本站源内执行脚本——而本站源可读写自选股与 LLM 配置接口。
   * 调用方拿到 '' 时应退化为纯文本，不要渲染成死链。
   */
  function safeUrl(u) {
    const s = String(u == null ? '' : u).trim();
    if (!s) return '';
    // 协议前允许出现控制字符/空白的绕过写法（如 "java\tscript:"），先剔除再判定
    const probe = s.replace(/[\u0000-\u0020]/g, '').toLowerCase();
    if (probe.indexOf('http://') === 0 || probe.indexOf('https://') === 0) return s;
    // 协议相对（//host/path）按 https 处理；站内相对路径同样放行
    if (probe.indexOf('//') === 0) return s;
    if (probe.charAt(0) === '/' || probe.charAt(0) === '?' || probe.charAt(0) === '#') return s;
    return '';
  }

  function debounce(fn, wait) {
    let timer = null;
    return function () {
      const args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait);
    };
  }

  function toast(message, kind) {
    const root = document.getElementById('toast-root');
    if (!root) return;
    const node = el('div', 'toast' + (kind ? ' ' + kind : ''), message);
    root.appendChild(node);
    setTimeout(function () {
      node.style.opacity = '0';
      node.style.transition = 'opacity .25s';
      setTimeout(function () { node.remove(); }, 250);
    }, 2600);
  }

  let openConfirm = null;   // 同一时刻只留一个确认气泡
  let confirmSeq = 0;

  /**
   * 就地确认气泡：贴在触发按钮旁边，用来替代 window.confirm。
   *
   * 原生 confirm 由浏览器画在窗口顶部正中，跟用户刚点的那个按钮离得很远——
   * 删自选股第 8 行时，视线要从表格底部跳到屏幕顶再跳回来，还容易看不清删的是哪只。
   * 这里把确认框锚到按钮上方（放不下就翻到下方），左右夹在视口内，箭头指向按钮中心。
   *
   * 用法与 confirm 一致，只是要 await：
   *   if (!await U.confirmAt(btn, '从自选股中删除 XX？', { okText: '删除' })) return;
   *
   * @param {Element} anchor  定位锚点，通常就是被点击的按钮
   * @param {string}  message 提示文案
   * @param {{okText?:string, cancelText?:string, danger?:boolean}} [opts]
   * @returns {Promise<boolean>} 确定 true / 取消·Esc·点外部·锚点消失 false
   */
  function confirmAt(anchor, message, opts) {
    const o = opts || {};
    if (openConfirm) openConfirm.close(false);

    return new Promise(function (resolve) {
      const pop = el('div', 'confirm-pop');
      pop.setAttribute('role', 'alertdialog');
      const msg = el('div', 'confirm-msg', message);
      msg.id = 'confirm-msg-' + (++confirmSeq);
      pop.setAttribute('aria-describedby', msg.id);
      pop.appendChild(msg);

      const row = el('div', 'confirm-actions');
      const cancel = el('button', 'btn btn-sm', o.cancelText || '取消');
      const ok = el('button', 'btn btn-sm ' + (o.danger === false ? 'btn-primary' : 'btn-danger'),
        o.okText || '确定');
      row.appendChild(cancel);
      row.appendChild(ok);
      pop.appendChild(row);

      const arrow = el('div', 'confirm-arrow');
      pop.appendChild(arrow);
      document.body.appendChild(pop);

      const prevFocus = document.activeElement;
      let done = false;

      function place() {
        // 轮询整表重载会把锚点按钮换掉：此时残留的气泡已经没有归属，直接当取消
        if (!anchor.isConnected) return close(false);
        const r = anchor.getBoundingClientRect();
        const pw = pop.offsetWidth, ph = pop.offsetHeight;
        const gap = 9, edge = 8;
        let top = r.top - ph - gap;
        const below = top < edge;
        if (below) top = r.bottom + gap;
        // 操作列整体右对齐，气泡也右对齐按钮，再夹回视口内
        const left = Math.max(edge, Math.min(r.right - pw, window.innerWidth - pw - edge));
        pop.style.top = Math.round(top) + 'px';
        pop.style.left = Math.round(left) + 'px';
        pop.classList.toggle('below', below);
        // 箭头指向按钮中心，但不越过气泡圆角
        arrow.style.left = Math.round(
          Math.min(Math.max(r.left + r.width / 2 - left, 15), Math.max(pw - 15, 15))) + 'px';
      }

      function close(result) {
        if (done) return;
        done = true;
        openConfirm = null;
        document.removeEventListener('keydown', onKey, true);
        document.removeEventListener('mousedown', onOutside, true);
        window.removeEventListener('resize', place);
        window.removeEventListener('scroll', place, true);
        pop.remove();
        if (prevFocus && prevFocus.isConnected) {
          try { prevFocus.focus(); } catch (e) { /* 元素可能已不可聚焦 */ }
        }
        resolve(result);
      }

      function onKey(e) {
        if (e.key !== 'Escape') return;
        e.stopPropagation();   // 别让 Esc 顺带关掉底下的弹窗
        close(false);
      }
      function onOutside(e) {
        if (!pop.contains(e.target)) close(false);
      }

      cancel.onclick = function () { close(false); };
      ok.onclick = function () { close(true); };

      place();
      // 捕获阶段：点外部要先于行/表格自己的 click 处理器生效；
      // 用 mousedown 而不是 click，免得把「打开气泡的这一次点击」的 click 也算成外部点击
      document.addEventListener('keydown', onKey, true);
      document.addEventListener('mousedown', onOutside, true);
      window.addEventListener('resize', place);
      window.addEventListener('scroll', place, true);   // 表格内部横向滚动也要跟着挪
      ok.focus();

      openConfirm = { close: close };
    });
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
    safeUrl: safeUrl,
    debounce: debounce,
    toast: toast,
    confirmAt: confirmAt,
    copyText: copyText
  };
})(window);
