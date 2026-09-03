// modal-root 弹窗单点控制：ai.js / news.js / page-hotspot.js 共用同一套
// #modal-root DOM（modal-title / modal-actions / modal-body）。
// 此前三个模块各自复制 root/body/title/actions/show/close 定义，
// 并各自注册一份 document 级 data-close / Esc 监听（3 份重复），现收敛于此。
// 使用方约定：需要「关闭时重置自身状态」的，用 onClose(fn) 注册回调。

function root() { return document.getElementById('modal-root'); }
function body() { return document.getElementById('modal-body'); }
function titleNode() { return document.getElementById('modal-title'); }
function actionsNode() { return document.getElementById('modal-actions'); }

function isOpen() { return !root().hidden; }

function show() {
  root().hidden = false;
  document.body.style.overflow = 'hidden';
}

const _closeHooks = [];

/** 注册关闭回调（如重置调用方的 state.loading）；close() 时统一触发。 */
function onClose(fn) { _closeHooks.push(fn); }

function close() {
  if (root().hidden) return;
  root().hidden = true;
  document.body.style.overflow = '';
  for (const fn of _closeHooks) {
    try { fn(); } catch (e) { /* ignore */ }
  }
}

// document 级关闭交互：此前在 ai/news/hotspot 三处重复注册，现单点一份
document.addEventListener('click', function (e) {
  if (e.target && e.target.getAttribute && e.target.getAttribute('data-close') === '1') close();
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && isOpen()) close();
});

export { root, body, titleNode, actionsNode, isOpen, show, close, onClose };
