/* 全站共用：格式化、fetch 封装、健康轮询、登出。
   登录态走 HttpOnly Cookie，前端拿不到也不需要拿，URL 里不出现任何凭据。 */

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function fmt(n) {
  if (n == null) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e4) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n * 100) / 100);
}

function ms(v) {
  if (!v) return '—';
  return v >= 1000 ? (v / 1000).toFixed(2) + 's' : Math.round(v) + 'ms';
}

function pct(part, total, digits) {
  return total ? (part / total * 100).toFixed(digits == null ? 1 : digits) + '%' : '—';
}

function ago(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return Math.round(s) + ' 秒前';
  if (s < 3600) return Math.round(s / 60) + ' 分钟前';
  if (s < 86400) return Math.round(s / 3600) + ' 小时前';
  return Math.round(s / 86400) + ' 天前';
}

function when(ts, withDate) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString('zh-CN', { hour12: false });
  return withDate === false ? time
    : String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0')
      + ' ' + time;
}

function duration(seconds) {
  const d = Math.floor(seconds / 86400);
  const parts = [
    String(Math.floor(seconds % 86400 / 3600)).padStart(2, '0'),
    String(Math.floor(seconds % 3600 / 60)).padStart(2, '0'),
    String(Math.floor(seconds % 60)).padStart(2, '0'),
  ];
  return (d ? d + ' 天 ' : '') + parts.join(':');
}

function toLogin() {
  location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
}

async function j(url, opt) {
  opt = opt || {};
  opt.headers = Object.assign({ 'Content-Type': 'application/json' }, opt.headers || {});
  const r = await fetch(url, opt);
  if (r.status === 401) { toLogin(); throw new Error('未登录'); }
  if (!r.ok) {
    let msg = r.status + ' ' + r.statusText;
    try {
      const body = await r.json();
      if (body.error) msg = typeof body.error === 'string' ? body.error : (body.error.message || msg);
      else if (body.detail) msg = typeof body.detail === 'string' ? body.detail : msg;
    } catch (_) { /* 不是 JSON，用状态行 */ }
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

async function download(url, filename) {
  const r = await fetch(url);
  if (r.status === 401) return toLogin();
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  }
}

/* ---------------- 常用片段 ---------------- */

function kpi(c) {
  return '<div class="kpi"><div class="kpi-top"><span class="kpi-ic ' + (c.tone || '') + '">'
    + ic(c.icon, 13) + '</span><span class="kpi-label">' + esc(c.label) + '</span></div>'
    + '<div class="kpi-value ' + (c.tone === 'acc' ? '' : c.tone || '') + '">' + c.value + '</div>'
    + (c.foot ? '<div class="kpi-foot">' + c.foot + '</div>' : '') + '</div>';
}

function renderKpis(id, items) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = items.map(kpi).join('');
}

/* 200 保持安静，异常才实心跳出来 */
function statusBadge(code) {
  if (code === 200) return '<span class="badge ok">200</span>';
  if (code === 0) return '<span class="badge solid bad">网络</span>';
  if (code >= 500) return '<span class="badge solid bad">' + code + '</span>';
  return '<span class="badge solid warn">' + code + '</span>';
}

/* 占比条，用于"谁占得多"这类对比 */
function progressBar(value, max, tone) {
  const p = max ? Math.round(value / max * 100) : 0;
  return '<div class="bar"><i class="' + (tone || 'acc') + '" style="width:'
    + Math.min(100, p) + '%"></i></div>';
}

/* 余量。健康时只给数字，低于四成才加进度条 —— 每行都画一条满格绿条等于没画。 */
function quota(left, limit) {
  const ratio = limit ? left / limit : 1;
  const cls = ratio <= 0 ? 'out' : ratio < 0.2 ? 'out' : ratio < 0.4 ? 'low' : '';
  const text = '<span class="quota mono ' + cls + '">' + fmt(left)
    + '<u>/' + fmt(limit) + '</u></span>';
  if (ratio >= 0.4) return text;
  return '<div class="bar-row">' + text
    + progressBar(left, limit, ratio < 0.2 ? 'bad' : 'warn') + '</div>';
}

/* ---------------- 全局交互 ---------------- */

const sideToggle = document.getElementById('side-toggle');
if (sideToggle) {
  sideToggle.addEventListener('click', function () {
    document.getElementById('side').classList.toggle('open');
  });
}

const logoutBtn = document.getElementById('btn-logout');
if (logoutBtn) {
  logoutBtn.addEventListener('click', async function () {
    await fetch('/auth/logout', { method: 'POST' });
    location.href = '/login';
  });
}

/* 还在用随机生成的初始密码时，在内容区顶部挂一条提示（本会话内可关掉）。 */
function weakPasswordCallout(show) {
  const existing = document.getElementById('weak-pw');
  if (!show || sessionStorage.getItem('hideWeakPw') === '1') {
    if (existing) existing.remove();
    return;
  }
  if (existing) return;
  const content = document.querySelector('.content');
  if (!content) return;
  const el = document.createElement('div');
  el.id = 'weak-pw';
  el.className = 'callout';
  el.innerHTML = ic('lock', 15)
    + '<span>你还在用首次启动随机生成的管理密码。可以在 <b>config.toml</b> 里写 '
    + '<b>admin_password</b>，或到<a href="/settings">设置页</a>改一个。</span>'
    + '<span class="spacer"></span>'
    + '<button class="btn sm ghost" id="weak-pw-x">知道了</button>';
  content.insertBefore(el, content.firstChild);
  document.getElementById('weak-pw-x').addEventListener('click', function () {
    sessionStorage.setItem('hideWeakPw', '1');
    el.remove();
  });
}

function setNavCount(id, text, warn) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'nav-count' + (warn ? ' warn' : '');
}

function pollHealth() {
  fetch('/health').then(function (r) { return r.json(); }).then(function (h) {
    const label = document.getElementById('status-text');
    const dot = document.getElementById('status-dot');
    if (!label) return;
    if (h && h.accounts != null) {
      label.textContent = h.enabled + '/' + h.accounts + ' 渠道可用'
        + (h.inflight ? ' · ' + h.inflight + ' 在途' : '');
      dot.className = 'dot live';
      setNavCount('nav-channels', h.enabled + '/' + h.accounts, h.enabled === 0);
      setNavCount('nav-models', String(h.models || 0));
      setNavCount('nav-tokens', h.client_auth ? '已保护' : '未保护', !h.client_auth);
      weakPasswordCallout(h.default_password);
    } else {
      label.textContent = '未登录';
      dot.className = 'dot off';
    }
  }).catch(function () {
    const dot = document.getElementById('status-dot');
    const label = document.getElementById('status-text');
    if (dot) dot.className = 'dot off';
    if (label) label.textContent = '服务离线';
  });
}

pollHealth();
setInterval(pollHealth, 10000);
