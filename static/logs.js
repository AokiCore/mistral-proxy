/* 调用日志：服务端分页 + 多条件筛选 */
let TIMER = null;
let LAST = { rows: [] };

const $ = (id) => document.getElementById(id);
const FILTERS = ['f-hours', 'f-status', 'f-endpoint', 'f-model', 'f-key', 'f-stream',
                 'f-search'];

const table = new DataTable('#tbl', {
  server: true,
  pageSize: 50,
  rowKey: (r) => String(r.id),
  empty: { icon: 'inbox', text: '没有符合条件的调用记录' },
  onRowClick: showDetail,
  columns: [
    { key: 'ts', label: '时间', mono: true, render: (r) => when(r.ts) },
    {
      key: 'status', label: '状态',
      render: (r) => statusBadge(r.status)
        + (r.attempts > 1 ? ' <span class="chip">重试' + (r.attempts - 1) + '</span>' : ''),
    },
    {
      key: 'requested_model', label: '模型', mono: true,
      render: (r) => esc(r.requested_model || r.model || '—')
        + (r.stream ? ' <span class="chip acc">流</span>' : ''),
    },
    {
      key: 'endpoint', label: '端点', mono: true,
      render: (r) => esc((r.endpoint || '').replace('/v1/', '')),
    },
    { key: 'account', label: '渠道', mono: true, render: (r) => esc(r.account || '—') },
    { key: 'client_name', label: '令牌', render: (r) => esc(r.client_name || '匿名') },
    { key: 'prompt_tokens', label: '输入', align: 'num', mono: true, render: (r) => fmt(r.prompt_tokens) },
    { key: 'completion_tokens', label: '输出', align: 'num', mono: true, render: (r) => fmt(r.completion_tokens) },
    {
      key: 'reasoning_tokens', label: '思考', align: 'num', mono: true,
      render: (r) => (r.reasoning_tokens ? fmt(r.reasoning_tokens) : '—'),
    },
    { key: 'ttft_ms', label: 'TTFT', align: 'num', mono: true, render: (r) => ms(r.ttft_ms) },
    { key: 'duration_ms', label: '耗时', align: 'num', mono: true, render: (r) => ms(r.duration_ms) },
    {
      key: 'error', label: '错误', wrap: true,
      render: (r) => r.error
        ? '<span class="mono" title="' + esc(r.error) + '">' + esc(r.error.slice(0, 48)) + '</span>'
        : '<span class="muted">—</span>',
    },
  ],
});

function query() {
  const q = new URLSearchParams({
    hours: $('f-hours').value, page: table.page, limit: table.pageSize,
  });
  const extras = {
    status: $('f-status').value, endpoint: $('f-endpoint').value,
    model: $('f-model').value, client_key: $('f-key').value,
    stream: $('f-stream').value, search: $('f-search').value.trim(),
  };
  Object.keys(extras).forEach((k) => { if (extras[k]) q.set(k, extras[k]); });
  return q.toString();
}

async function load() {
  try {
    const d = await j('/admin/logs?' + query());
    LAST = d;
    table.setServerData(d.rows, d.total, d.page);
    $('subtitle').textContent = fmt(d.total) + ' 条记录 · ' + pct(d.ok, d.total)
      + ' 成功 · ' + fmt(d.sum_tokens) + ' tokens';
  } catch (e) {
    if (e.message !== '未登录') toast('加载失败: ' + e.message, 'bad');
  }
}

async function loadFilters() {
  try {
    const f = await j('/admin/logs/filters');
    fill('f-endpoint', f.endpoints.map((v) => ({ v, t: v })), '全部端点');
    fill('f-model', f.models.map((v) => ({ v, t: v })), '全部模型');
    fill('f-key', f.keys.map((k) => ({ v: k.id, t: k.name })), '全部令牌');
  } catch (_) { /* 筛选项加载失败不影响主表 */ }
}

function fill(id, items, placeholder) {
  const sel = $(id);
  const current = sel.value;
  sel.innerHTML = '<option value="">' + placeholder + '</option>'
    + items.map((i) => '<option value="' + esc(i.v) + '">' + esc(i.t) + '</option>').join('');
  sel.value = current;
}

function showDetail(row) {
  $('detail-sub').textContent = when(row.ts) + ' · ' + (row.endpoint || '');
  const props = [
    ['状态码', row.status], ['尝试次数', row.attempts],
    ['请求模型', row.requested_model], ['上游模型', row.model],
    ['上游渠道', row.account], ['访问令牌', row.client_name || '匿名'],
    ['流式', row.stream ? '是' : '否'], ['结束原因', row.finish_reason || '—'],
    ['输入 tokens', row.prompt_tokens], ['输出 tokens', row.completion_tokens],
    ['思考 tokens', row.reasoning_tokens], ['缓存命中 tokens', row.cached_tokens],
    ['合计 tokens', row.total_tokens],
    ['首字节 TTFT', ms(row.ttft_ms)], ['端到端耗时', ms(row.duration_ms)],
    ['错误信息', row.error || '（无）'],
  ];
  $('detail-props').innerHTML = props.map(
    (p) => '<dt>' + esc(p[0]) + '</dt><dd>' + esc(p[1] == null ? '—' : p[1]) + '</dd>').join('');
  openModal('detail-modal');
}

FILTERS.forEach((id) => {
  const el = $(id);
  const evt = el.tagName === 'INPUT' ? 'input' : 'change';
  let timer = null;
  el.addEventListener(evt, () => {
    clearTimeout(timer);
    timer = setTimeout(() => { table.page = 1; load(); }, evt === 'input' ? 320 : 0);
  });
});

$('tbl').addEventListener('pagechange', load);
$('btn-reset').addEventListener('click', () => {
  $('f-hours').value = '24';
  ['f-status', 'f-endpoint', 'f-model', 'f-key', 'f-stream'].forEach((id) => { $(id).value = ''; });
  $('f-search').value = '';
  table.page = 1;
  load();
});
$('btn-refresh').addEventListener('click', load);
$('btn-export').addEventListener('click', () => {
  const hours = $('f-hours').value;
  download('/admin/export?hours=' + hours + '&model=' + encodeURIComponent($('f-model').value),
           'mistral_logs_' + hours + 'h.csv')
    .catch((e) => toast('导出失败: ' + e.message, 'bad'));
});
$('auto').addEventListener('change', () => {
  clearInterval(TIMER);
  TIMER = $('auto').checked ? setInterval(load, 5000) : null;
});

loadFilters();
load();
