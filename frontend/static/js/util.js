/* util（从 IIFE+global 转 ESM） */

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

  /** 时分秒：HH:MM:SS，用于"获取于 HH:MM:SS"这类时间戳 */
  function fmtTime(d) {
    const dt = d instanceof Date ? d : new Date();
    const pad = function (n) { return n < 10 ? '0' + n : '' + n; };
    return pad(dt.getHours()) + ':' + pad(dt.getMinutes()) + ':' + pad(dt.getSeconds());
  }
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

  export const U = {
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
    fmtTime: fmtTime,
    el: el,
    escapeHtml: escapeHtml,
    safeUrl: safeUrl,
    debounce: debounce,
    toast: toast,
    confirmAt: confirmAt,
    copyText: copyText,
    icon: icon,
    iconHtml: iconHtml
  };
/* SVG 图标库：替代 emoji/Unicode 符号，避免精简容器里渲染成方块 */
const SVG_NS = 'http://www.w3.org/2000/svg';
const ICONS = {
alert: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><polyline points="21 3 21 8 16 8"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><polyline points="3 21 3 16 8 16"/>',
x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 0 1-4 0v-.09A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 0 1 0-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1.03-1.56V3a2 2 0 0 1 4 0v.09A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9c.14.32.22.66.22 1.03s-.08.71-.22 1.03"/>',
flame: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
diamond: '<path d="M6 5h12l3 5-8.5 9.5a.7.7 0 0 1-1 0L3 10z"/><path d="M3.7 10h16.6"/><path d="m11 5 1.5 5L11 15"/><path d="m13 5-1.5 5L13 15"/>',
robot: '<rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 4v4"/><circle cx="9" cy="13" r="1"/><circle cx="15" cy="13" r="1"/><path d="M9 17h6"/>',
chartBar: '<line x1="3" y1="3" x2="3" y2="21"/><line x1="21" y1="3" x2="21" y2="21"/><line x1="21" y1="3" x2="3" y2="3"/><rect x="7" y="13" width="3" height="5"/><rect x="12" y="9" width="3" height="9"/><rect x="17" y="5" width="3" height="13"/>',
chartLine: '<path d="M3 3v18h18"/><polyline points="7 14 11 10 14 13 21 6"/>',
search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
news: '<rect x="6" y="4" width="14" height="16" rx="1"/><path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h10"/><path d="M19 18a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2"/>',
fileText: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="14" y2="17"/>',
inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
bolt: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
info: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
arrowUp: '<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>',
arrowDown: '<line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/>',
arrowRight: '<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>',
eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
star: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>'
};

/** 创建 SVG 图标 DOM 节点（返回 <svg>，可直接 appendChild）。 */
function icon(name, opts) {
const path = ICONS[name];
const o = opts || {};
const sz = o.size || 16;
const cls = o.cls || 'svg-icon';
const sw = o.stroke || 2;
if (!path) return document.createTextNode('?');
const svg = document.createElementNS(SVG_NS, 'svg');
svg.setAttribute('class', cls);
svg.setAttribute('viewBox', '0 0 24 24');
svg.setAttribute('width', String(sz));
svg.setAttribute('height', String(sz));
svg.setAttribute('fill', 'none');
svg.setAttribute('stroke', 'currentColor');
svg.setAttribute('stroke-width', String(sw));
svg.setAttribute('stroke-linecap', 'round');
svg.setAttribute('stroke-linejoin', 'round');
if (o.title) { svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', o.title); }
else { svg.setAttribute('aria-hidden', 'true'); }
svg.innerHTML = path;
return svg;
}

/** 字符串版：用在内嵌 innerHTML 模板里。 */
function iconHtml(name, opts) {
const path = ICONS[name];
const o = opts || {};
if (!path) return escapeHtml(name || '?');
const sz = o.size || 16;
const cls = o.cls || 'svg-icon';
const sw = o.stroke || 2;
const a11y = o.title ? ' role="img" aria-label="' + escapeHtml(o.title) + '"' : ' aria-hidden="true"';
const attrs = ' fill="none" stroke="currentColor" stroke-width="' + sw + '" stroke-linecap="round" stroke-linejoin="round"' + a11y;
return '<svg class="' + cls + '" viewBox="0 0 24 24" width="' + sz + '" height="' + sz + '"' + attrs + '>' + path + '</svg>';
}
