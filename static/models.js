/* 模型注册表 */
let MODELS = [];
let ALIASES = {};

const $ = (id) => document.getElementById(id);

const CAP_LABELS = {
  completion_chat: '对话', reasoning: '推理', vision: '视觉', function_calling: '工具',
  ocr: 'OCR', audio: '音频', moderation: '审核', completion_fim: 'FIM',
  classification: '分类', fine_tuning: '微调', audio_transcription: '转录',
  audio_speech: '语音合成',
};

const table = new DataTable('#tbl', {
  pageSize: 20,
  rowKey: (r) => r.id,
  defaultSort: { key: 'id', dir: 'asc' },
  empty: { icon: 'cube', text: '没有匹配的模型' },
  columns: [
    {
      key: 'id', label: '模型 ID', mono: true, sort: true,
      render: (r) => esc(r.id)
        + (r.deprecation ? ' <span class="badge warn">将下线</span>' : ''),
    },
    {
      key: 'capabilities', label: '能力', wrap: true,
      render: (r) => Object.keys(CAP_LABELS).filter((k) => r.capabilities[k])
        .map((k) => '<span class="chip acc">' + CAP_LABELS[k] + '</span>').join('')
        || '<span class="muted">—</span>',
    },
    {
      key: 'context', label: '上下文', sort: true, align: 'num', mono: true,
      render: (r) => (r.context ? fmt(r.context) : '—'),
    },
    {
      key: 'aliases', label: '上游别名', wrap: true, mono: true,
      render: (r) => {
        const others = (r.aliases || []).filter((a) => a !== r.id);
        return others.length ? esc(others.join(', ')) : '<span class="muted">—</span>';
      },
    },
    { key: 'description', label: '说明', wrap: true, render: (r) => esc(r.description || '—') },
  ],
});

const aliasTable = new DataTable('#alias-tbl', {
  pageSize: 10,
  rowKey: (r) => r.alias,
  empty: { icon: 'cube', text: '还没有自定义别名' },
  columns: [
    { key: 'alias', label: '别名', mono: true, sort: true },
    { key: 'target', label: '指向', mono: true, sort: true },
    {
      key: '_act', label: '操作', width: '90px',
      render: (r) => '<button class="btn sm danger" data-alias="' + esc(r.alias) + '">删除</button>',
    },
  ],
});

function applyFilter() {
  const q = $('q').value.trim().toLowerCase();
  const cap = $('cap').value;
  table.setFilter((m) => (!q || m.id.toLowerCase().includes(q)
    || (m.description || '').toLowerCase().includes(q)) && (!cap || m.capabilities[cap]));
}

async function load() {
  try {
    const d = await j('/admin/models');
    MODELS = d.models;
    ALIASES = d.aliases || {};
    table.setRows(MODELS);
    applyFilter();
    aliasTable.setRows(Object.entries(ALIASES).map(([alias, target]) => ({ alias, target })));

    $('subtitle').textContent = d.synced_at
      ? MODELS.length + ' 个模型 · 上次同步 ' + ago(d.synced_at) : '尚未同步';

    const count = (c) => MODELS.filter((m) => m.capabilities[c]).length;
    renderKpis('kpis', [
      {
        icon: 'cube', label: '模型总数', value: MODELS.length,
        foot: Object.keys(ALIASES).length + ' 个自定义别名',
      },
      { icon: 'activity', label: '支持对话', value: count('completion_chat') },
      { icon: 'brain', label: '支持推理', value: count('reasoning'), tone: 'acc',
        foot: '可用 reasoning_effort' },
      { icon: 'eye', label: '支持视觉', value: count('vision') },
      { icon: 'zap', label: '支持工具调用', value: count('function_calling') },
    ]);

    const sel = $('a-target');
    const current = sel.value;
    sel.innerHTML = MODELS.filter((m) => m.capabilities.completion_chat)
      .map((m) => '<option value="' + esc(m.id) + '">' + esc(m.id) + '</option>').join('');
    if (current) sel.value = current;
  } catch (e) {
    if (e.message !== '未登录') toast('加载失败: ' + e.message, 'bad');
  }
}

$('q').addEventListener('input', applyFilter);
$('cap').addEventListener('change', applyFilter);
$('btn-alias').addEventListener('click', () => openModal('alias-modal'));

$('btn-sync').addEventListener('click', async () => {
  $('btn-sync').disabled = true;
  try {
    const r = await j('/admin/models/sync', { method: 'POST' });
    toast(r.synced ? '已同步 ' + r.synced + ' 个模型' : '同步失败，保留了旧数据',
          r.synced ? 'ok' : 'bad');
    load();
  } catch (e) {
    toast(e.message, 'bad');
  } finally {
    $('btn-sync').disabled = false;
  }
});

$('do-alias').addEventListener('click', async () => {
  const err = $('a-err');
  try {
    await j('/admin/models/alias', {
      method: 'POST',
      body: JSON.stringify({ alias: $('a-alias').value.trim(), target: $('a-target').value }),
    });
    err.textContent = '';
    $('a-alias').value = '';
    closeModals();
    toast('别名已添加', 'ok');
    load();
  } catch (e) {
    err.textContent = e.message;
  }
});

$('alias-tbl').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-alias]');
  if (!btn) return;
  try {
    await j('/admin/models/alias', {
      method: 'POST', body: JSON.stringify({ action: 'remove', alias: btn.dataset.alias }),
    });
    toast('别名已删除', 'ok');
    load();
  } catch (e) {
    toast(e.message, 'bad');
  }
});

load();
