/* 设置页 */
const $ = (id) => document.getElementById(id);
let CONFIG = {};

function runtimeProps(c) {
  return [
    ['运行时长', duration(c.uptime_s)],
    ['配置文件', c.config_file || '（未使用）'],
    ['监听地址', c.host + ':' + c.port],
    ['并发上限', c.max_concurrency + '（改配置文件后重启生效）'],
    ['上游读超时', c.read_timeout + 's'],
    ['已同步模型', c.model_count],
    ['调用方鉴权', c.client_auth ? '已开启' : '未开启（去「访问令牌」页签发即可开启）'],
    ['数据库', c.db_path],
    ['账号文件', c.keys_file || '（未使用）'],
    ['用量写入丢弃', c.dropped_usage_rows + ' 行'],
  ];
}

function passwordPanel(c) {
  if (c.password_source !== 'database') {
    return '<div class="callout info" style="margin:0">' + ic('info', 15)
      + '<span>当前密码来自 <b>' + esc(c.password_source_label) + '</b>，'
      + '它的优先级高于数据库，所以这里改不了 —— 直接改那个来源再重启即可。<br>'
      + '想改成在界面里管理，把那里的密码留空或删掉，重启后会改用数据库里的密码。</span></div>';
  }
  return (c.default_password
      ? '<div class="callout" style="margin-bottom:14px">' + ic('alert', 15)
        + '<span>当前用的还是首次启动随机生成的密码，建议现在改掉。</span></div>'
      : '')
    + '<p class="muted" style="line-height:1.7;margin-bottom:14px">来源：'
    + esc(c.password_source_label) + '。以 PBKDF2（20 万轮）散列存在本地数据库，'
    + '改完所有设备的登录会立刻失效。也可以在配置文件里写 <code>admin_password</code> 固定住，'
    + '或用命令行 <code>python app.py --set-password</code>。</p>'
    + '<div class="form-group"><label class="field">当前密码</label>'
    + '<input type="password" id="pw-current" autocomplete="current-password"></div>'
    + '<div class="form-group"><label class="field">新密码（至少 6 位）</label>'
    + '<input type="password" id="pw-new" autocomplete="new-password"></div>'
    + '<div class="form-group"><label class="field">再输一次</label>'
    + '<input type="password" id="pw-again" autocomplete="new-password"></div>'
    + '<div class="bar-row"><button class="btn primary" id="do-password">修改密码</button>'
    + '<span class="field-error" id="pw-err"></span></div>';
}

async function load() {
  try {
    CONFIG = await j('/admin/config');
    $('reasoning-format').value = CONFIG.reasoning_format;
    $('max-retry').value = CONFIG.max_retry_accounts;
    $('runtime').innerHTML = runtimeProps(CONFIG).map(
      (r) => '<dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd>').join('');
    $('password-box').innerHTML = passwordPanel(CONFIG);
    const btn = $('do-password');
    if (btn) btn.addEventListener('click', changePassword);

    $('endpoints').textContent =
      'Chat        POST ' + location.origin + '/v1/chat/completions\n'
      + 'Embeddings  POST ' + location.origin + '/v1/embeddings\n'
      + 'Moderations POST ' + location.origin + '/v1/moderations\n'
      + 'Models      GET  ' + location.origin + '/v1/models\n'
      + 'Health      GET  ' + location.origin + '/health\n\n'
      + 'OpenAI 客户端里把 base_url 填成 ' + location.origin + '/v1';
  } catch (e) {
    if (e.message !== '未登录') toast('加载失败: ' + e.message, 'bad');
  }
}

async function changePassword() {
  const err = $('pw-err');
  err.textContent = '';
  const next = $('pw-new').value;
  if (next !== $('pw-again').value) { err.textContent = '两次输入的新密码不一致'; return; }
  if (next.length < 6) { err.textContent = '新密码至少 6 位'; return; }
  try {
    const r = await j('/admin/password', {
      method: 'POST',
      body: JSON.stringify({ current: $('pw-current').value, new: next }),
    });
    toast(r.message, 'ok');
    setTimeout(() => { location.href = '/login'; }, 1500);
  } catch (e) {
    err.textContent = e.message;
  }
}

$('save-config').addEventListener('click', async () => {
  try {
    await j('/admin/config', {
      method: 'POST',
      body: JSON.stringify({
        reasoning_format: $('reasoning-format').value,
        max_retry_accounts: parseInt($('max-retry').value, 10) || 4,
      }),
    });
    toast('配置已生效', 'ok');
    load();
  } catch (e) {
    toast(e.message, 'bad');
  }
});

$('revoke-sessions').addEventListener('click', async () => {
  if (!confirm('让所有设备（包括当前这台）的登录立刻失效？')) return;
  try {
    await j('/admin/sessions/revoke', { method: 'POST' });
    location.href = '/login';
  } catch (e) {
    toast(e.message, 'bad');
  }
});

$('do-cleanup').addEventListener('click', async () => {
  const days = $('cleanup-days').value;
  if (!confirm('删除 ' + days + ' 天以前的所有调用日志？不可恢复。')) return;
  try {
    const r = await j('/admin/cleanup', {
      method: 'POST', body: JSON.stringify({ days: parseInt(days, 10) }),
    });
    toast('已删除 ' + fmt(r.deleted) + ' 条记录', 'ok');
  } catch (e) {
    toast(e.message, 'bad');
  }
});

$('export-all').addEventListener('click', () => {
  download('/admin/export?hours=720', 'mistral_usage_30d.csv')
    .catch((e) => toast('导出失败: ' + e.message, 'bad'));
});

load();
