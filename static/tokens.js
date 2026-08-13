/* 访问令牌（签发给下游调用方的密钥） */
let KEYS = [];
let EDITING = null;

const $ = (id) => document.getElementById(id);

function tokenBadge(k) {
  if (!k.enabled) return '<span class="badge">已禁用</span>';
  if (k.expired) return '<span class="badge bad">已过期</span>';
  return '<span class="badge ok">生效中</span>';
}

function limitCell(v, unit) {
  return v ? '<span class="mono">' + fmt(v) + unit + '</span>' : '<span class="muted">不限</span>';
}

const table = new DataTable('#tbl', {
  selectable: true,
  pageSize: 20,
  rowKey: (r) => r.id,
  defaultSort: { key: 'created_at', dir: 'desc' },
  empty: { icon: 'key', text: '还没有令牌，点右上角签发一把' },
  columns: [
    { key: 'name', label: '名称', sort: true },
    { key: 'prefix', label: '前缀', mono: true, render: (r) => esc(r.prefix) + '…' },
    {
      key: 'enabled', label: '状态', sort: true,
      sortValue: (r) => (r.enabled ? (r.expired ? 1 : 0) : 2),
      render: tokenBadge,
    },
    { key: 'rpm_limit', label: '速率上限', sort: true, render: (r) => limitCell(r.rpm_limit, '/min') },
    {
      key: 'daily_token_limit', label: '日 token 配额', sort: true,
      render: (r) => (r.daily_token_limit
        ? quota(Math.max(0, r.daily_token_limit - r.today.tokens), r.daily_token_limit)
        : '<span class="muted">不限</span>'),
    },
    {
      key: 'allowed_models', label: '模型白名单', wrap: true,
      render: (r) => r.allowed_models.length
        ? r.allowed_models.map((m) => '<span class="chip">' + esc(m) + '</span>').join('')
        : '<span class="muted">全部</span>',
    },
    {
      key: 'today_requests', label: '今日请求', sort: true, align: 'num', mono: true,
      sortValue: (r) => r.today.requests, render: (r) => fmt(r.today.requests),
    },
    {
      key: 'today_tokens', label: '今日 tokens', sort: true, align: 'num', mono: true,
      sortValue: (r) => r.today.tokens, render: (r) => fmt(r.today.tokens),
    },
    { key: 'last_used', label: '最后使用', sort: true, render: (r) => ago(r.last_used) },
    { key: 'created_at', label: '创建于', sort: true, render: (r) => when(r.created_at) },
    {
      key: '_act', label: '操作', width: '150px',
      render: (r) => '<div class="bar-row">'
        + '<button class="btn sm" data-act="edit" data-id="' + esc(r.id) + '">编辑</button>'
        + '<button class="btn sm" data-act="' + (r.enabled ? 'disable' : 'enable')
        + '" data-id="' + esc(r.id) + '">' + (r.enabled ? '禁用' : '启用') + '</button>'
        + '<button class="btn sm danger" data-act="revoke" data-id="' + esc(r.id)
        + '">吊销</button></div>',
    },
  ],
});

function syncSelection() {
  const n = table.selected().length;
  $('sel-count').textContent = n ? '已选 ' + n + ' 把' : '';
  ['btn-enable', 'btn-disable', 'btn-revoke'].forEach((id) => { $(id).disabled = !n; });
}

async function load() {
  try {
    const d = await j('/admin/keys');
    KEYS = d.keys;
    table.setRows(KEYS);
    applyFilter();
    $('open-callout').style.display = d.auth_required ? 'none' : 'flex';

    const active = KEYS.filter((k) => k.enabled && !k.expired).length;
    renderKpis('kpis', [
      { icon: 'key', label: '令牌总数', value: KEYS.length, foot: active + ' 把生效中' },
      {
        icon: d.auth_required ? 'lock' : 'unlock', label: '调用方鉴权',
        value: d.auth_required ? '已开启' : '未开启',
        tone: d.auth_required ? 'ok' : 'bad',
        foot: d.static_key_configured ? '含配置文件里的固定密钥' : '签发令牌后自动开启',
      },
      {
        icon: 'activity', label: '今日请求',
        value: fmt(KEYS.reduce((a, k) => a + k.today.requests, 0)),
      },
      {
        icon: 'layers', label: '今日 tokens',
        value: fmt(KEYS.reduce((a, k) => a + k.today.tokens, 0)),
      },
    ]);
  } catch (e) {
    if (e.message !== '未登录') toast('加载失败: ' + e.message, 'bad');
  }
  $('usage').textContent =
    'base_url : ' + location.origin + '/v1\n'
    + 'api_key  : sk-pool-…（下面签发的令牌）\n\n'
    + 'curl ' + location.origin + '/v1/chat/completions \\\n'
    + '  -H "Authorization: Bearer sk-pool-..." \\\n'
    + '  -H "Content-Type: application/json" \\\n'
    + '  -d \'{"model":"mistral-small-latest","messages":[{"role":"user","content":"hi"}]}\'';
}

function applyFilter() {
  const q = $('q').value.trim().toLowerCase();
  table.setFilter((k) => !q || k.name.toLowerCase().includes(q)
    || k.prefix.toLowerCase().includes(q));
}

const parseModels = (v) => v.split(',').map((s) => s.trim()).filter(Boolean);

async function act(ids, action) {
  if (!ids.length) return;
  if (action === 'revoke' && !confirm('吊销 ' + ids.length + ' 把令牌？使用中的客户端会立刻 401。')) return;
  try {
    const r = await j('/admin/keys/action', { method: 'POST', body: JSON.stringify({ ids, action }) });
    toast('已' + { enable: '启用', disable: '禁用', revoke: '吊销' }[action] + ' ' + r.ok + ' 把', 'ok');
    table.clearSelection();
    syncSelection();
    load();
  } catch (e) {
    toast(e.message, 'bad');
  }
}

function openEdit(id) {
  const k = KEYS.find((x) => x.id === id);
  if (!k) return;
  EDITING = id;
  $('edit-sub').textContent = '前缀 ' + k.prefix + '… · 累计 ' + fmt(k.total_requests)
    + ' 次请求 / ' + fmt(k.total_tokens) + ' tokens';
  $('e-name').value = k.name;
  $('e-rpm').value = k.rpm_limit;
  $('e-quota').value = k.daily_token_limit;
  $('e-models').value = k.allowed_models.join(', ');
  openModal('edit-modal');
}

$('q').addEventListener('input', applyFilter);
$('tbl').addEventListener('selectionchange', syncSelection);
$('tbl').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  if (btn.dataset.act === 'edit') openEdit(btn.dataset.id);
  else act([btn.dataset.id], btn.dataset.act);
});
$('btn-enable').addEventListener('click', () => act(table.selected(), 'enable'));
$('btn-disable').addEventListener('click', () => act(table.selected(), 'disable'));
$('btn-revoke').addEventListener('click', () => act(table.selected(), 'revoke'));
$('btn-new').addEventListener('click', () => openModal('new-modal'));

$('do-new').addEventListener('click', async () => {
  const err = $('new-err');
  try {
    const r = await j('/admin/keys', {
      method: 'POST',
      body: JSON.stringify({
        name: $('k-name').value.trim(),
        rpm_limit: parseInt($('k-rpm').value, 10) || 0,
        daily_token_limit: parseInt($('k-quota').value, 10) || 0,
        ttl_days: parseInt($('k-ttl').value, 10) || 0,
        allowed_models: parseModels($('k-models').value),
      }),
    });
    err.textContent = '';
    closeModals();
    $('new-key').textContent = r.key;
    openModal('show-modal');
    $('k-name').value = '';
    $('k-models').value = '';
    load();
  } catch (e) {
    err.textContent = e.message;
  }
});

$('do-edit').addEventListener('click', async () => {
  const err = $('edit-err');
  try {
    await j('/admin/keys/update', {
      method: 'POST',
      body: JSON.stringify({
        id: EDITING, name: $('e-name').value.trim(),
        rpm_limit: parseInt($('e-rpm').value, 10) || 0,
        daily_token_limit: parseInt($('e-quota').value, 10) || 0,
        allowed_models: parseModels($('e-models').value),
      }),
    });
    err.textContent = '';
    closeModals();
    toast('已保存', 'ok');
    load();
  } catch (e) {
    err.textContent = e.message;
  }
});

$('copy-key').addEventListener('click', async () => {
  const ok = await copyText($('new-key').textContent);
  $('copy-key').textContent = ok ? '已复制' : '复制失败';
  setTimeout(() => { $('copy-key').textContent = '复制'; }, 2000);
});

load();
setInterval(load, 20000);
