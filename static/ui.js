/* 通用 UI：主题、Toast、弹窗、以及带排序/分页/选择的 DataTable。
   四个列表页共用同一套表格逻辑，避免每页各拼一遍 HTML 字符串。 */

/* ---------------- 主题 ---------------- */
const Theme = {
  get() {
    return localStorage.getItem('theme')
      || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  },
  apply(name) {
    document.documentElement.dataset.theme = name;
    localStorage.setItem('theme', name);
    document.querySelectorAll('[data-theme-icon]').forEach(function (el) {
      el.innerHTML = ic(name === 'dark' ? 'sun' : 'moon', 15);
    });
  },
  toggle() { Theme.apply(Theme.get() === 'dark' ? 'light' : 'dark'); },
};
Theme.apply(Theme.get());
document.addEventListener('click', function (ev) {
  if (ev.target.closest('#theme-toggle')) Theme.toggle();
});

/* ---------------- Toast ---------------- */
function toast(message, kind) {
  let host = document.querySelector('.toasts');
  if (!host) {
    host = document.createElement('div');
    host.className = 'toasts';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.innerHTML = ic(kind === 'bad' ? 'alert' : kind === 'ok' ? 'check-circle' : 'info', 15)
    + '<span>' + esc(message) + '</span>';
  host.appendChild(el);
  setTimeout(function () {
    el.classList.add('leaving');
    setTimeout(function () { el.remove(); }, 200);
  }, kind === 'bad' ? 6000 : 3600);
}

/* ---------------- 弹窗 ---------------- */
function openModal(id) {
  const m = document.getElementById(id);
  if (!m) return;
  m.classList.add('show');
  const first = m.querySelector('input:not([type=checkbox]), textarea, select');
  if (first) setTimeout(function () { first.focus(); }, 40);
}

function closeModals() {
  document.querySelectorAll('.modal.show').forEach(function (m) { m.classList.remove('show'); });
}

document.addEventListener('click', function (ev) {
  if (ev.target.closest('[data-close]')) closeModals();
  if (ev.target.classList.contains('modal')) closeModals();
});
document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Escape') closeModals();
});

/* ---------------- DataTable ---------------- */
const PAGE_SIZES = [10, 20, 50, 100];

class DataTable {
  /**
   * columns: [{ key, label, render?, sort?, sortValue?, align?, mono?, wrap?, width? }]
   * options: { pageSize, selectable, rowKey, empty, onRowClick, defaultSort, server }
   * server 模式下由调用方通过 setServerData() 喂数据，排序/分页交给后端。
   */
  constructor(container, options) {
    this.el = typeof container === 'string' ? document.querySelector(container) : container;
    this.columns = options.columns;
    this.rowKey = options.rowKey || function (r, i) { return String(i); };
    this.pageSize = options.pageSize || 20;
    this.selectable = !!options.selectable;
    this.emptyState = options.empty || { icon: 'inbox', text: '暂无数据' };
    this.onRowClick = options.onRowClick || null;
    this.server = options.server || null;
    this.sort = options.defaultSort || null;
    this.rows = [];
    this.total = 0;
    this.page = 1;
    this.loading = true;
    this.selection = new Set();
    this.filterFn = null;
    this._bind();
  }

  _bind() {
    const self = this;
    this.el.addEventListener('click', function (ev) {
      const th = ev.target.closest('th.sortable');
      if (th) { self._toggleSort(th.dataset.key); return; }

      const pageBtn = ev.target.closest('[data-page]');
      if (pageBtn && !pageBtn.disabled) { self.goto(parseInt(pageBtn.dataset.page, 10)); return; }

      const all = ev.target.closest('[data-select-all]');
      if (all) { self._selectAll(all.checked); return; }

      const box = ev.target.closest('[data-select-row]');
      if (box) {
        if (box.checked) self.selection.add(box.value);
        else self.selection.delete(box.value);
        self._syncSelectAll();
        self.el.dispatchEvent(new CustomEvent('selectionchange',
          { detail: self.selected() }));
        return;
      }

      if (self.onRowClick) {
        const tr = ev.target.closest('tr[data-key]');
        if (tr && !ev.target.closest('button, input, a')) {
          const row = self.visibleRows().find(function (r, i) {
            return self.rowKey(r, i) === tr.dataset.key;
          });
          if (row) self.onRowClick(row);
        }
      }
    });
    this.el.addEventListener('change', function (ev) {
      const sel = ev.target.closest('[data-page-size]');
      if (sel) {
        self.pageSize = parseInt(sel.value, 10);
        self.page = 1;
        self.el.dispatchEvent(new CustomEvent('pagechange',
          { detail: { page: 1, pageSize: self.pageSize } }));
        if (!self.server) self.render();
      }
    });
  }

  /* ---- 数据 ---- */

  setRows(rows) {
    this.rows = rows || [];
    this.total = this.rows.length;
    this.loading = false;
    const maxPage = Math.max(1, Math.ceil(this.filtered().length / this.pageSize));
    if (this.page > maxPage) this.page = maxPage;
    this.render();
  }

  setServerData(rows, total, page) {
    this.rows = rows || [];
    this.total = total || 0;
    this.page = page || 1;
    this.loading = false;
    this.render();
  }

  setLoading(on) {
    this.loading = on !== false;
    this.render();
  }

  setFilter(fn) {
    this.filterFn = fn;
    this.page = 1;
    this.render();
  }

  filtered() {
    return this.filterFn ? this.rows.filter(this.filterFn) : this.rows;
  }

  sorted() {
    const rows = this.filtered().slice();
    if (!this.sort) return rows;
    const col = this.columns.find((c) => c.key === this.sort.key);
    if (!col) return rows;
    const valueOf = col.sortValue || function (r) { return r[col.key]; };
    const dir = this.sort.dir === 'asc' ? 1 : -1;
    return rows.sort(function (a, b) {
      const x = valueOf(a), y = valueOf(b);
      if (x == null) return 1;
      if (y == null) return -1;
      if (typeof x === 'number' && typeof y === 'number') return (x - y) * dir;
      return String(x).localeCompare(String(y), 'zh-CN') * dir;
    });
  }

  visibleRows() {
    if (this.server) return this.rows;
    const start = (this.page - 1) * this.pageSize;
    return this.sorted().slice(start, start + this.pageSize);
  }

  count() { return this.server ? this.total : this.filtered().length; }
  pages() { return Math.max(1, Math.ceil(this.count() / this.pageSize)); }

  goto(page) {
    const target = Math.min(this.pages(), Math.max(1, page));
    if (target === this.page) return;
    this.page = target;
    this.el.dispatchEvent(new CustomEvent('pagechange',
      { detail: { page: target, pageSize: this.pageSize } }));
    if (!this.server) this.render();
  }

  _toggleSort(key) {
    const col = this.columns.find((c) => c.key === key);
    if (!col || !col.sort) return;
    if (this.sort && this.sort.key === key) {
      this.sort = { key: key, dir: this.sort.dir === 'asc' ? 'desc' : 'asc' };
    } else {
      this.sort = { key: key, dir: 'desc' };
    }
    this.page = 1;
    this.el.dispatchEvent(new CustomEvent('sortchange', { detail: this.sort }));
    if (!this.server) this.render();
  }

  /* ---- 选择 ---- */

  selected() { return Array.from(this.selection); }
  clearSelection() { this.selection.clear(); this.render(); }

  _selectAll(on) {
    const self = this;
    this.visibleRows().forEach(function (row, i) {
      const key = self.rowKey(row, i);
      if (on) self.selection.add(key); else self.selection.delete(key);
    });
    this.render();
    this.el.dispatchEvent(new CustomEvent('selectionchange', { detail: this.selected() }));
  }

  _syncSelectAll() {
    const box = this.el.querySelector('[data-select-all]');
    if (!box) return;
    const keys = this.visibleRows().map(this.rowKey);
    box.checked = keys.length > 0 && keys.every((k) => this.selection.has(k));
  }

  /* ---- 渲染 ---- */

  render() {
    const cols = this.columns.length + (this.selectable ? 1 : 0);
    this.el.innerHTML = '<div class="tbl-scroll"><table>'
      + this._head() + this._body(cols) + '</table></div>' + this._pager();
    this._syncSelectAll();
  }

  _head() {
    const self = this;
    const cells = this.columns.map(function (c) {
      const attrs = [];
      if (c.sort) attrs.push('class="sortable' + (c.align === 'num' ? ' num' : '') + '"');
      else if (c.align === 'num') attrs.push('class="num"');
      attrs.push('data-key="' + esc(c.key) + '"');
      if (c.width) attrs.push('style="width:' + c.width + '"');
      const active = self.sort && self.sort.key === c.key;
      if (active) attrs.push('aria-sort="' + (self.sort.dir === 'asc' ? 'ascending' : 'descending') + '"');
      const mark = c.sort
        ? '<span class="sort-mark">' + (active ? (self.sort.dir === 'asc' ? '▲' : '▼') : '↕') + '</span>'
        : '';
      return '<th ' + attrs.join(' ') + '>' + esc(c.label) + mark + '</th>';
    });
    if (this.selectable) {
      cells.unshift('<th style="width:34px"><input type="checkbox" data-select-all></th>');
    }
    return '<thead><tr>' + cells.join('') + '</tr></thead>';
  }

  _body(colSpan) {
    if (this.loading) return '<tbody>' + this._skeleton(colSpan) + '</tbody>';
    const rows = this.visibleRows();
    if (!rows.length) {
      return '<tbody><tr><td colspan="' + colSpan + '" style="padding:0">'
        + '<div class="empty-state">' + ic(this.emptyState.icon || 'inbox', 30)
        + '<p>' + esc(this.emptyState.text) + '</p></div></td></tr></tbody>';
    }
    const self = this;
    return '<tbody>' + rows.map(function (row, i) {
      const key = self.rowKey(row, i);
      const cells = self.columns.map(function (c) {
        const classes = [];
        if (c.align === 'num') classes.push('num');
        if (c.mono) classes.push('mono');
        if (c.wrap) classes.push('wrap');
        const value = c.render ? c.render(row) : esc(row[c.key] == null ? '-' : row[c.key]);
        return '<td' + (classes.length ? ' class="' + classes.join(' ') + '"' : '') + '>'
          + value + '</td>';
      });
      if (self.selectable) {
        cells.unshift('<td><input type="checkbox" data-select-row value="' + esc(key) + '"'
          + (self.selection.has(key) ? ' checked' : '') + '></td>');
      }
      return '<tr data-key="' + esc(key) + '"'
        + (self.onRowClick ? ' class="row-link"' : '') + '>' + cells.join('') + '</tr>';
    }).join('') + '</tbody>';
  }

  _skeleton(colSpan) {
    let out = '';
    for (let r = 0; r < 6; r++) {
      out += '<tr><td colspan="' + colSpan + '"><div class="skeleton" style="width:'
        + (55 + Math.random() * 40) + '%"></div></td></tr>';
    }
    return out;
  }

  _pager() {
    const total = this.count();
    // 空表已经有空状态说明了，再挂一条"无记录 / 1 / »"的分页条纯属噪声
    if (!total) return '';
    const pages = this.pages();
    const from = (this.page - 1) * this.pageSize + 1;
    const to = Math.min(total, this.page * this.pageSize);
    const info = '第 ' + from + '–' + to + ' 条，共 ' + fmt(total) + ' 条';

    const sizes = PAGE_SIZES.map((n) =>
      '<option value="' + n + '"' + (n === this.pageSize ? ' selected' : '') + '>'
      + n + ' 条/页</option>').join('');

    let numbers = '';
    for (const p of pageWindow(this.page, pages)) {
      numbers += p === '…'
        ? '<span class="page-gap">…</span>'
        : '<button class="page-btn" data-page="' + p + '"'
          + (p === this.page ? ' aria-current="true"' : '') + '>' + p + '</button>';
    }

    return '<div class="pager"><span class="info">' + info + '</span>'
      + '<select data-page-size>' + sizes + '</select>'
      + '<button class="page-btn" data-page="1"' + (this.page <= 1 ? ' disabled' : '') + '>«</button>'
      + '<button class="page-btn" data-page="' + (this.page - 1) + '"'
      + (this.page <= 1 ? ' disabled' : '') + '>‹</button>'
      + numbers
      + '<button class="page-btn" data-page="' + (this.page + 1) + '"'
      + (this.page >= pages ? ' disabled' : '') + '>›</button>'
      + '<button class="page-btn" data-page="' + pages + '"'
      + (this.page >= pages ? ' disabled' : '') + '>»</button></div>';
  }
}

/* 页码窗口：首页、末页、当前页附近，中间用省略号。 */
function pageWindow(current, total) {
  if (total <= 7) return Array.from({ length: total }, function (_, i) { return i + 1; });
  const out = [1];
  const start = Math.max(2, current - 1);
  const end = Math.min(total - 1, current + 1);
  if (start > 2) out.push('…');
  for (let p = start; p <= end; p++) out.push(p);
  if (end < total - 1) out.push('…');
  out.push(total);
  return out;
}
