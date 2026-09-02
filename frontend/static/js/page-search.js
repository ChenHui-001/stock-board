/* page-search（从 IIFE+global 转 ESM） */
import { U } from './util.js';
import { API } from './api.js';


  const state = { keyword: '', items: [], hot: null, loading: false };

  function view() { return document.getElementById('view'); }

  function render() {
    const root = view();
    root.innerHTML = '';

    // 搜索框
    const box = U.el('div', 'search-box');
    const searchIcon = document.createElement('span');
    searchIcon.className = 'search-icon';
    searchIcon.appendChild(U.icon('search', { size: 16 }));
    box.appendChild(searchIcon);
    const input = U.el('input', 'search-input');
    input.id = 'search-input';
    input.placeholder = '输入股票名称、6 位代码或拼音首字母，如：浦发银行 / 600000 / pfyh';
    input.autocomplete = 'off';
    input.maxLength = 32;               // 与后端 /api/search 的 max_length 对齐
    input.value = state.keyword;
    input.addEventListener('input', onInput);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') doSearch(input.value);
    });
    box.appendChild(input);
    root.appendChild(box);

    root.appendChild(U.el('div', 'search-hint',
      '支持模糊搜索与拼音首字母，输入即时联想匹配。'));

    const resultHost = U.el('div');
    resultHost.id = 'search-result';
    resultHost.style.marginTop = '14px';
    root.appendChild(resultHost);

    const hotHost = U.el('div');
    hotHost.id = 'hot-host';
    hotHost.style.marginTop = '18px';
    root.appendChild(hotHost);

    renderResult();
    renderHot();

    setTimeout(function () { input.focus(); }, 30);
  }

  const onInput = U.debounce(function (e) {
    doSearch(e.target.value);
  }, 280);

  async function doSearch(keyword) {
    state.keyword = keyword;
    const kw = (keyword || '').trim();
    if (!kw) {
      state.items = [];
      renderResult();
      return;
    }
    state.loading = true;
    renderResult();
    try {
      const data = await API.search(kw, 15);
      // 输入框内容可能已经变了，丢弃过期响应
      if ((state.keyword || '').trim() !== kw) return;
      state.items = data.items || [];
    } catch (err) {
      state.items = [];
      U.toast('搜索失败：' + err.message, 'err');
    } finally {
      state.loading = false;
      renderResult();
    }
  }

  function renderResult() {
    const host = document.getElementById('search-result');
    if (!host) return;
    host.innerHTML = '';

    const kw = (state.keyword || '').trim();
    if (!kw) return;

    const card = U.el('div', 'card');
    const head = U.el('div', 'card-head');
    head.appendChild(U.el('div', 'card-title', '搜索结果'));
    head.appendChild(U.el('div', 'card-sub', state.loading ? '匹配中…' : (state.items.length + ' 条')));
    card.appendChild(head);

    if (state.loading) {
      card.appendChild(U.el('div', 'loading-block', '正在匹配…'));
    } else if (!state.items.length) {
      const empty = U.el('div', 'empty');
      const sIcon = U.el('div', 'empty-icon');
      sIcon.appendChild(U.icon('search', { size: 40 }));
      empty.appendChild(sIcon);
      empty.appendChild(U.el('div', 'empty-title', '暂无匹配股票'));
      empty.appendChild(U.el('div', 'empty-desc', '换个名称、代码或拼音首字母试试。'));
      card.appendChild(empty);
    } else {
      state.items.forEach(function (item) {
        card.appendChild(renderResultRow(item));
      });
    }
    host.appendChild(card);
  }

  function renderResultRow(item) {
    const row = U.el('div', 'result-row');

    const cell = U.el('div', 'stock-cell');
    cell.appendChild(U.el('div', 'stock-name', item.name || item.code));
    cell.appendChild(U.el('div', 'stock-code', item.code + ' · ' + item.market));
    row.appendChild(cell);

    const boardCell = U.el('div', 'board-cell');
    const tags = U.el('div', 'board-tags');
    tags.appendChild(U.el('span', 'tag', item.board || '--'));
    boardCell.appendChild(tags);
    row.appendChild(boardCell);

    const priceCell = U.el('div', 'price-cell ' + U.tone(item.change_pct), U.price(item.price));
    row.appendChild(priceCell);

    row.appendChild(U.el('div', 'pct-cell ' + U.tone(item.change_pct), U.pct(item.change_pct)));

    const actions = U.el('div', 'row-actions');
    const btn = U.el('button', 'btn btn-sm' + (item.watched ? '' : ' btn-primary'));
    btn.textContent = item.watched ? '已自选' : '添加自选';
    btn.disabled = !!item.watched;
    btn.onclick = function () { addWatch(item, btn); };
    actions.appendChild(btn);
    row.appendChild(actions);

    return row;
  }

  async function addWatch(item, btn) {
    btn.disabled = true;
    btn.classList.add('loading');
    try {
      await API.addWatch(item.code, item.name, item.board);
      item.watched = true;
      btn.classList.remove('loading', 'btn-primary');
      btn.textContent = '已自选';
      U.toast('已添加「' + (item.name || item.code) + '」到自选', 'ok');
      // 同步热门榜里的同一只股票
      if (state.hot) {
        ['gainers', 'losers', 'actives'].forEach(function (k) {
          (state.hot[k] || []).forEach(function (h) {
            if (h.code === item.code) h.watched = true;
          });
        });
        renderHot();
      }
    } catch (err) {
      btn.disabled = false;
      btn.classList.remove('loading');
      U.toast('添加失败：' + err.message, 'err');
    }
  }

  async function renderHot() {
    const host = document.getElementById('hot-host');
    if (!host) return;

    if (!state.hot) {
      host.innerHTML = '<div class="card"><div class="loading-block">加载热门股票…</div></div>';
      try {
        state.hot = await API.hot(8);
      } catch (err) {
        host.innerHTML = '<div class="card"><div class="loading-block">热门榜暂不可用</div></div>';
        return;
      }
      if (!document.getElementById('hot-host')) return;
    }

    host.innerHTML = '';
    const title = U.el('div', 'card-sub');
    title.style.marginBottom = '8px';
    title.textContent = state.hot.stale
      ? '当日热门股票（数据源限流，展示最近一次成功数据）'
      : '当日热门股票';
    host.appendChild(title);

    const grid = U.el('div', 'hot-grid');
    grid.appendChild(hotCard('涨幅榜', state.hot.gainers, 'change_pct'));
    grid.appendChild(hotCard('跌幅榜', state.hot.losers, 'change_pct'));
    grid.appendChild(hotCard('成交额榜', state.hot.actives, 'amount'));
    host.appendChild(grid);
  }

  function hotCard(title, items, valueKey) {
    const card = U.el('div', 'card');
    const head = U.el('div', 'card-head');
    head.appendChild(U.el('div', 'card-title', title));
    card.appendChild(head);

    if (!items || !items.length) {
      card.appendChild(U.el('div', 'loading-block', '暂无数据'));
      return card;
    }

    items.forEach(function (item, idx) {
      const row = U.el('div', 'hot-row');
      const rank = U.el('div', 'hot-rank' + (idx < 3 ? ' top' : ''), String(idx + 1));
      row.appendChild(rank);

      const name = U.el('div', 'hot-name');
      name.textContent = item.name || item.code;
      name.title = item.code + (item.board ? ' · ' + item.board : '');
      row.appendChild(name);

      const val = valueKey === 'amount'
        ? U.el('div', 'hot-val', U.money(item.amount, 1))
        : U.el('div', 'hot-val ' + U.tone(item.change_pct), U.pct(item.change_pct));
      row.appendChild(val);

      const btn = U.el('button', 'btn btn-sm', item.watched ? '已选' : '+ 自选');
      btn.disabled = !!item.watched;
      btn.onclick = function () { addWatch(item, btn); };
      row.appendChild(btn);

      card.appendChild(row);
    });
    return card;
  }

  export const PageSearch = {
    mount: function () {
      state.hot = null;
      render();
    },
    refresh: async function () {
      state.hot = null;
      await renderHot();
      if ((state.keyword || '').trim()) await doSearch(state.keyword);
    },
    tick: function () { /* 查询页不做自动刷新 */ }
  };
