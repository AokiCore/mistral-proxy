/* 上游渠道（Mistral 账号池）。表格排序、分页、批量选择都交给 DataTable。 */
let ACCOUNTS = [];
let REVEAL = false;

const $ = (id) => document.getElementById(id);

function state(a) {
  if (!a.enabled) return 'disabled';
  if (a.exhausted) return 'exhausted';
  if (a.cooling) return 'cooling';
  if (a.remaining_req <= 0) return 'drained';
  return 'ok';
}

const STATE_BADGE = {
  disabled: '<span class="badge">已禁用</span>',
  exhausted: '<span class="badge bad">月额度尽</span>',
  cooling: '<span class="badge bad">冷却中</span>',
  drained: '<span class="badge warn">窗口用尽</span>',
  ok: '<span class="badge ok">可用</span>',
};

/* 免费档发的是每月美元额度，不是 token 配额；花光后整号 402 直到次月 1 号。 */
function budgetCell(a) {
  if (!a.budget_checked_at) return '<span class="muted">未查</span>';
  const left = (a.budget_total * (1 - a.budget_used_pct / 100)).toFixed(2);
  const cls = a.budget_used_pct >= 100 ? 'out' : (a.budget_used_pct >= 80 ? 'low' : '');
  return '<span class="quota ' + cls + '">$' + left + '</span>'
    + '<span class="muted"> / $' + a.budget_total + '</span>';
}

/* 把嵌套的 Account.orgs[] 展平成行，每行带 email + org 信息 */
function flattenAccounts(accounts) {
  const rows = [];
  for (const acc of accounts) {
    for (const org of (acc.orgs || [])) {
      rows.push({ ...org, email: acc.email, enabled: acc.enabled });
    }
  }
  return rows;
}

/* 浏览器里读文件，只把内容传上去；服务端永远不接触本地路径。 */
let PENDING = '';

function readFiles(files) {
  const list = Array.from(files || []).filter(Boolean);
  if (!list.length) return;
  const preview = $('imp-preview');
  Promise.all(list.map((f) => f.text().then((text) => ({ name: f.name, size: f.size, text }))))
    .then((results) => {
      const merged = [];
      const lines = [];
      let bad = 0;
      for (const r of results) {
        let count = 0;
        try {
          count = countRecords(r.text);
        } catch (_) { count = -1; }
        if (count > 0) merged.push(r.text);
        else bad++;
        lines.push('<div class="file">' + ic(count > 0 ? 'check-circle' : 'alert', 14)
          + '<span style="margin:0 auto 0 0">' + esc(r.name) + '</span>'
          + '<span class="' + (count > 0 ? '' : 'bad') + '">'
          + (count > 0 ? count + ' 个账号' : '解析不出账号') + '</span></div>');
      }
      PENDING = mergeContents(merged);
      preview.style.display = 'block';
      preview.innerHTML = lines.join('');
      $('imp-err').textContent = bad === results.length ? '这些文件里没有可用的账号' : '';
    });
}

/* 只是数一下条数给用户看，真正的解析在服务端做 */
function countRecords(text) {
  const trimmed = text.trim();
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    const data = JSON.parse(trimmed);
    const arr = Array.isArray(data) ? data : [data];
    return arr.filter((r) => r && r.api_key).length;
  }
  const rows = trimmed.split(/\r?\n/).filter((l) => l.trim());
  if (rows.length < 2 || !/api_key/i.test(rows[0])) return 0;
  return rows.length - 1;
}

/* 多个文件合并成一个 JSON 数组再提交，避免来回请求 */
function mergeContents(texts) {
  if (!texts.length) return '';
  if (texts.length === 1) return texts[0];
  const all = [];
  for (const t of texts) {
    const trimmed = t.trim();
    if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
      const data = JSON.parse(trimmed);
      all.push(...(Array.isArray(data) ? data : [data]));
    } else {
      const rows = trimmed.split(/\r?\n/).filter((l) => l.trim());
      const head = rows[0].split(',').map((h) => h.trim());
      for (const line of rows.slice(1)) {
        const cells = line.split(',');
        const obj = {};
        head.forEach((h, i) => { obj[h] = (cells[i] || '').trim(); });
        all.push(obj);
      }
    }
  }
  return JSON.stringify(all);
}

const table = new DataTable('#tbl', {
  selectable: true,
  pageSize: 20,
  rowKey: (r) => r.uid,
  defaultSort: { key: 'email', dir: 'asc' },
  empty: { icon: 'server', text: '没有匹配的渠道' },
  columns: [
    { key: 'email', label: '邮箱', mono: true, sort: true },
    {
      key: 'api_key', label: 'API Key', mono: true,
      render: (r) => esc(REVEAL && r.api_key ? r.api_key : r.key_preview),
    },
    {
      key: 'status', label: '状态', sort: true,
      sortValue: (r) => ['ok', 'drained', 'cooling', 'disabled'].indexOf(state(r)),
      render: (r) => STATE_BADGE[state(r)],
    },
    {
      key: 'budget', label: '月度额度', sort: true, align: 'num',
      sortValue: (r) => (r.budget_checked_at ? -r.budget_used_pct : 1),
      render: budgetCell,
    },
    {
      key: 'remaining_tokens', label: 'tokens 余量', sort: true, align: 'num',
      render: (r) => quota(r.remaining_tokens, r.limit_tokens),
    },
    {
      key: 'remaining_req', label: '请求余量', sort: true, align: 'num',
      render: (r) => quota(r.remaining_req, r.limit_req),
    },
    {
      key: 'inflight', label: '在途', sort: true, align: 'num', mono: true,
      render: (r) => (r.inflight ? r.inflight : '<span class="muted">0</span>'),
    },
    {
      key: 'window_reset_at', label: '窗口重置', sort: true, mono: true,
      render: (r) => when(r.window_reset_at, false),
    },
    {
      key: 'last_status', label: '最近状态',
      render: (r) => '<span class="muted">' + esc(r.last_status) + '</span>',
    },
    {
      key: 'consecutive_errors', label: '连续错误', sort: true, align: 'num',
      render: (r) => (r.consecutive_errors
        ? '<span class="quota out">' + r.consecutive_errors + '</span>'
        : '<span class="muted">0</span>'),
    },
    {
      key: '_act', label: '操作', width: '128px',
      render: (r) => '<div class="bar-row">'
        + '<button class="btn sm" data-act="' + (r.enabled ? 'disable' : 'enable')
        + '" data-uid="' + esc(r.uid) + '">' + (r.enabled ? '禁用' : '启用') + '</button>'
        + '<button class="btn sm danger" data-act="remove" data-uid="' + esc(r.uid)
        + '">删除</button></div>',
    },
  ],
});

function applyFilter() {
  const q = $('q').value.trim().toLowerCase();
  const f = $('filter').value;
  table.setFilter((a) => (!q || a.email.toLowerCase().includes(q)) && (!f || state(a) === f));
  updateSubtitle();
}

function updateSubtitle() {
  const shown = table.count();
  $('subtitle').textContent = shown === ACCOUNTS.length
    ? ACCOUNTS.length + ' 个渠道'
    : '筛选出 ' + shown + ' 个，共 ' + ACCOUNTS.length + ' 个';
}

function syncSelection() {
  const n = table.selected().length;
  $('sel-count').textContent = n ? '已选 ' + n + ' 个' : '';
  ['btn-enable', 'btn-disable', 'btn-remove', 'btn-budget']
    .forEach((id) => { $(id).disabled = !n; });
}

/* 查额度要登控制台，一个号约 2.5 秒，所以限量并给出进度提示。 */
async function refreshBudget(uids) {
  if (!uids.length) return;
  if (uids.length > 20) {
    toast('一次最多查 20 个，选少点', 'warn');
    return;
  }
  const btn = $('btn-budget');
  btn.disabled = true;
  btn.textContent = '查询中…';
  try {
    /* uid 是 api_key 的 hash，但 budget 查询需要 email，从行数据里取 */
    const emails = uids.map((uid) => {
      const row = ACCOUNTS.find((a) => a.uid === uid);
      return row ? row.email : '';
    }).filter(Boolean);
    const r = await j('/admin/accounts/budget', {
      method: 'POST', body: JSON.stringify({ emails }),
    });
    const bad = r.failed.length;
    toast('查到 ' + r.checked.length + ' 个' + (bad ? '，' + bad + ' 个失败' : ''),
      bad ? 'warn' : 'ok');
    if (bad) console.warn('额度查询失败:', r.failed);
    load();
  } catch (e) {
    toast(e.message, 'bad');
  } finally {
    btn.textContent = '查额度';
    syncSelection();
  }
}

async function load() {
  try {
    const d = await j('/admin/accounts' + (REVEAL ? '?reveal=1' : ''));
    ACCOUNTS = flattenAccounts(d.accounts);
    table.setRows(ACCOUNTS);
    applyFilter();

    const s = d.summary;
    renderKpis('kpis', [
      { icon: 'server', label: '渠道总数', value: s.total, foot: s.enabled + ' 个启用' },
      { icon: 'layers', label: '组织数', value: s.orgs || 0, foot: '多组织多 Key' },
      {
        icon: 'check-circle', label: '当前可用', tone: 'ok',
        value: s.enabled - s.cooling - s.drained, foot: '未冷却且有额度',
      },
      {
        icon: 'clock', label: '冷却中', value: s.cooling, tone: s.cooling ? 'warn' : '',
        foot: '撞过 429，等窗口重置',
      },
      {
        icon: 'alert', label: '月额度用尽', value: s.exhausted || 0,
        tone: s.exhausted ? 'bad' : '', foot: '次月 1 号自动恢复',
      },
      { icon: 'inflight', label: '在途请求', value: s.inflight, tone: 'acc' },
      {
        icon: 'zap', label: '剩余月额度',
        value: s.budget_checked ? '$' + s.budget_left : '—',
        foot: s.budget_checked
          ? '已查 ' + s.budget_checked + ' 个号，共 $' + s.budget_total
          : '还没查过额度',
      },
      { icon: 'layers', label: '每分钟 token 余量', value: fmt(s.tokens_left) },
    ]);
  } catch (e) {
    if (e.message !== '未登录') toast('加载失败: ' + e.message, 'bad');
  }
}

async function act(uids, action) {
  if (!uids.length) return;
  if (action === 'remove' && !confirm('删除 ' + uids.length
    + ' 个渠道？会写入墓碑记录，重启后不会被 keys 文件重新导入。')) return;
  try {
    /* enable/disable/remove 按 email 操作（账号级别），从 uid 反查 email */
    const emails = uids.map((uid) => {
      const row = ACCOUNTS.find((a) => a.uid === uid);
      return row ? row.email : '';
    }).filter(Boolean);
    const unique = [...new Set(emails)];
    const r = await j('/admin/accounts/action', {
      method: 'POST', body: JSON.stringify({ emails: unique, action }),
    });
    toast('已' + { enable: '启用', disable: '禁用', remove: '删除', restore: '恢复' }[action]
      + ' ' + r.ok + ' 个渠道', 'ok');
    table.clearSelection();
    syncSelection();
    load();
  } catch (e) {
    toast(e.message, 'bad');
  }
}

$('q').addEventListener('input', applyFilter);
$('filter').addEventListener('change', applyFilter);
$('tbl').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (btn) act([btn.dataset.uid], btn.dataset.act);
});
$('tbl').addEventListener('selectionchange', syncSelection);
$('btn-enable').addEventListener('click', () => act(table.selected(), 'enable'));
$('btn-disable').addEventListener('click', () => act(table.selected(), 'disable'));
$('btn-remove').addEventListener('click', () => act(table.selected(), 'remove'));
$('btn-budget').addEventListener('click', () => refreshBudget(table.selected()));
$('btn-reveal').addEventListener('click', () => {
  REVEAL = !REVEAL;
  $('btn-reveal').textContent = REVEAL ? '隐藏 Key' : '显示完整 Key';
  load();
});
$('btn-import').addEventListener('click', () => openModal('imp-modal'));
$('btn-add').addEventListener('click', () => openModal('add-modal'));

/* 文件选择与拖放 */
const dz = $('dropzone');
dz.addEventListener('click', () => $('imp-file').click());
$('imp-file').addEventListener('change', (ev) => readFiles(ev.target.files));
['dragenter', 'dragover'].forEach((e) => dz.addEventListener(e, (ev) => {
  ev.preventDefault();
  dz.classList.add('over');
}));
['dragleave', 'drop'].forEach((e) => dz.addEventListener(e, (ev) => {
  ev.preventDefault();
  dz.classList.remove('over');
}));
dz.addEventListener('drop', (ev) => readFiles(ev.dataTransfer.files));

$('do-import').addEventListener('click', async () => {
  const pasted = $('imp-content').value.trim();
  const content = PENDING || pasted;
  const restore = $('imp-restore').value.split(',').map((s) => s.trim()).filter(Boolean);
  const err = $('imp-err');
  if (!content && !restore.length) { err.textContent = '请选择文件、粘贴内容，或填写要恢复的邮箱'; return; }
  try {
    const parts = [];
    if (restore.length) {
      await j('/admin/accounts/action', {
        method: 'POST', body: JSON.stringify({ emails: restore, action: 'restore' }),
      });
      parts.push('恢复 ' + restore.length + ' 个已删除邮箱');
    }
    if (content) {
      const r = await j('/admin/accounts/import', {
        method: 'POST', body: JSON.stringify({ content }),
      });
      parts.push('新增 ' + r.added + '，更新 ' + r.updated);
      if (r.blocked) parts.push(r.blocked + ' 个被墓碑挡住');
      if (r.skipped) parts.push(r.skipped + ' 条无 api_key 已跳过');
    }
    err.textContent = '';
    PENDING = '';
    $('imp-content').value = '';
    $('imp-restore').value = '';
    $('imp-file').value = '';
    $('imp-preview').style.display = 'none';
    closeModals();
    toast(parts.join('，'), 'ok');
    load();
  } catch (e) {
    err.textContent = e.message;
  }
});

$('do-add').addEventListener('click', async () => {
  const err = $('add-err');
  const email = $('add-email').value.trim();
  const key = $('add-key').value.trim();
  if (!email || !key) { err.textContent = '邮箱和 API Key 都要填'; return; }
  try {
    const r = await j('/admin/accounts', {
      method: 'POST', body: JSON.stringify({ email, api_key: key }),
    });
    err.textContent = '';
    $('add-email').value = '';
    $('add-key').value = '';
    closeModals();
    toast('已添加，共 ' + r.total + ' 个渠道', 'ok');
    load();
  } catch (e) {
    err.textContent = e.message;
  }
});

load();
setInterval(load, 15000);
