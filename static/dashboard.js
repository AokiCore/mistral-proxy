/* 仪表盘：4 张主指标（带环比与迷你趋势）+ 一条次要指标 strip + 主图表 */
const HOURS = parseInt(new URLSearchParams(location.search).get('hours'), 10) || 24;
let POINTS = [];

const $ = (id) => document.getElementById(id);

/* 环比。上一窗口为 0 时不显示百分比（除以 0 出来的 ∞% 没有信息量）。 */
function delta(now, before, higherIsBetter) {
  if (!before) return '';
  const change = (now - before) / before * 100;
  if (Math.abs(change) < 0.5) return '<span class="delta flat">持平</span>';
  const up = change > 0;
  const good = higherIsBetter === false ? !up : up;
  return '<span class="delta ' + (good ? 'up' : 'down') + '">'
    + (up ? '↑' : '↓') + Math.abs(change).toFixed(1) + '%</span>';
}

function heroCard(c) {
  return '<div class="hero-card">'
    + '<div class="hero-head">' + ic(c.icon, 14) + esc(c.label) + '</div>'
    + '<div class="hero-value ' + (c.tone || '') + '">' + c.value
    + (c.unit ? '<small>' + c.unit + '</small>' : '') + '</div>'
    + '<div class="hero-foot">' + (c.delta || '') + '<span>' + (c.foot || '') + '</span>'
    + (c.spark || '') + '</div></div>';
}

function heroCards(s, series) {
  const w = s.overview.window;
  const prev = s.overview.previous;
  const rate = w.n ? w.ok / w.n * 100 : 0;
  const prevRate = prev.n ? prev.ok / prev.n * 100 : 0;
  const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  const okColor = getComputedStyle(document.documentElement).getPropertyValue('--ok').trim();
  const badColor = getComputedStyle(document.documentElement).getPropertyValue('--bad').trim();

  return [
    {
      icon: 'activity', label: '请求数', value: fmt(w.n),
      delta: delta(w.n, prev.n), foot: '较上一周期',
      spark: sparkline(series.requests, accent),
    },
    {
      icon: 'check-circle', label: '成功率',
      value: w.n ? rate.toFixed(1) : '—', unit: w.n ? '%' : '',
      tone: !w.n ? '' : rate >= 99 ? 'ok' : rate >= 90 ? 'warn' : 'bad',
      delta: prevRate ? delta(rate, prevRate) : '',
      foot: fmt(w.n - w.ok) + ' 次失败',
      spark: sparkline(series.errors, badColor),
    },
    {
      icon: 'layers', label: 'Tokens', value: fmt(w.tok),
      delta: delta(w.tok, prev.tok),
      foot: fmt(w.ptok) + ' 入 / ' + fmt(w.ctok) + ' 出',
      spark: sparkline(series.tokens, accent),
    },
    {
      icon: 'clock', label: 'P95 延迟', value: ms(s.latency.duration.p95).replace(/[a-z]+$/, ''),
      unit: s.latency.duration.p95 >= 1000 ? 's' : 'ms',
      delta: delta(w.avg_ms, prev.avg_ms, false), foot: '均值 ' + ms(w.avg_ms),
      spark: sparkline(series.latency, okColor),
    },
  ];
}

function stripItems(s, pool) {
  const w = s.overview.window;
  const items = [
    ['思考 tokens', fmt(w.rtok), w.ctok ? pct(w.rtok, w.ctok, 0) + ' 输出' : '', ''],
    ['缓存命中', fmt(w.cachetok), w.ptok ? pct(w.cachetok, w.ptok, 0) + ' 输入' : '', ''],
    ['流式请求', fmt(w.streamed), w.n ? pct(w.streamed, w.n, 0) : '', ''],
    ['限流 429', fmt(w.r429), '', w.r429 ? 'warn' : ''],
    ['服务端错误', fmt(w.r5xx), '', w.r5xx ? 'bad' : ''],
    ['换号重试', fmt(w.retries), '', ''],
    ['首字节 P50', ms(s.latency.ttft.p50), '流式', ''],
    ['今日请求', fmt(s.overview.today.n), fmt(s.overview.today.tok) + ' tok', ''],
    ['累计请求', fmt(s.overview.all.n), fmt(s.overview.all.tok) + ' tok', ''],
  ];
  if (pool) {
    items.push(['可用渠道', (pool.enabled - pool.cooling - pool.drained) + '/' + pool.total,
                pool.cooling + ' 冷却', pool.enabled ? '' : 'bad']);
    items.push(['在途请求', fmt(pool.inflight), fmt(pool.requests_left) + ' 余量', '']);
  }
  return items.map((it) => '<div class="strip-item"><span>' + esc(it[0]) + '</span>'
    + '<b class="' + it[3] + '">' + it[1] + '</b>'
    + (it[2] ? '<i>' + esc(it[2]) + '</i>' : '') + '</div>').join('');
}

function pctRow(id, p) {
  const el = $(id);
  if (!p || !p.n) {
    el.innerHTML = '<div class="muted" style="grid-column:1/-1;padding:8px 0">暂无样本</div>';
    return;
  }
  el.innerHTML = ['p50', 'p90', 'p95', 'p99'].map(
    (k) => '<div class="stat-cell"><span>' + k.toUpperCase() + '</span><b>' + ms(p[k]) + '</b></div>'
  ).join('');
}

function simpleTable(head, rows, empty) {
  if (!rows) return '<div class="empty-state"><p>' + empty + '</p></div>';
  return '<div class="tbl-scroll" style="max-height:330px"><table><thead><tr>' + head
    + '</tr></thead><tbody>' + rows + '</tbody></table></div>';
}

async function load() {
  try {
    const s = await j('/admin/stats?hours=' + HOURS);
    const pool = s.pool;
    $('subtitle').textContent = '最后更新 ' + when(Date.now() / 1000, false);

    const daily = s.bucket >= 86400;
    const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    const badColor = getComputedStyle(document.documentElement).getPropertyValue('--bad').trim();

    POINTS = s.by_time.map((b) => {
      const d = new Date(b.t * 1000);
      return {
        value: b.n, error: (b.r429 || 0) + (b.err || 0),
        label: daily ? (d.getMonth() + 1) + '/' + d.getDate()
          : String(d.getHours()).padStart(2, '0') + ':00',
        title: d.toLocaleString('zh-CN', { hour12: false }),
        rows: [
          [accent, '请求', fmt(b.n)],
          [badColor, '失败', fmt((b.r429 || 0) + (b.err || 0))],
          ['var(--text-3)', 'tokens', fmt(b.tok)],
          ['var(--text-3)', '均耗时', ms(b.avg_ms)],
        ],
      };
    });

    $('hero').innerHTML = heroCards(s, {
      requests: s.by_time.map((b) => b.n),
      tokens: s.by_time.map((b) => b.tok),
      errors: s.by_time.map((b) => (b.r429 || 0) + (b.err || 0)),
      latency: s.by_time.map((b) => b.avg_ms),
    }).map(heroCard).join('');
    $('strip').innerHTML = stripItems(s, pool);

    lineChart($('chart'), POINTS);
    $('chart-note').textContent = daily ? '按天聚合' : '按小时聚合';

    pctRow('pct-duration', s.latency.duration);
    pctRow('pct-ttft', s.latency.ttft);

    const statusTotal = s.by_status.reduce((a, r) => a + r.n, 0);
    const epTotal = s.by_endpoint.reduce((a, r) => a + r.n, 0);
    $('dist').innerHTML =
      '<div class="muted" style="margin-bottom:8px">状态码</div>'
      + barList(s.by_status.map((r) => ({
        badge: statusBadge(r.status), value: r.n,
        tone: r.status === 200 ? 'ok' : 'bad', share: pct(r.n, statusTotal),
      })))
      + '<div class="muted" style="margin:18px 0 8px">端点</div>'
      + barList(s.by_endpoint.map((r) => ({
        badge: '<span class="mono">' + esc(r.endpoint.replace('/v1/', '')) + '</span>',
        value: r.n, share: pct(r.n, epTotal),
      })));

    const maxTok = Math.max(1, ...s.by_model.map((m) => m.tok));
    $('models').innerHTML = simpleTable(
      '<th>模型</th><th class="num">请求</th><th class="num">tokens</th>'
      + '<th class="num">思考</th><th class="num">成功率</th><th class="num">均耗时</th><th></th>',
      s.by_model.map((m) => '<tr><td class="mono">' + esc(m.model || '(未知)') + '</td>'
        + '<td class="num">' + fmt(m.n) + '</td><td class="num">' + fmt(m.tok) + '</td>'
        + '<td class="num">' + (m.rtok ? fmt(m.rtok) : '—') + '</td>'
        + '<td class="num">' + pct(m.ok, m.n) + '</td>'
        + '<td class="num">' + ms(m.avg_ms) + '</td>'
        + '<td style="width:80px">' + progressBar(m.tok, maxTok) + '</td></tr>').join(''),
      '暂无数据');

    $('clients').innerHTML = simpleTable(
      '<th>令牌</th><th class="num">请求</th><th class="num">tokens</th>'
      + '<th class="num">成功率</th><th>最后使用</th>',
      (s.by_client || []).map((c) => '<tr><td>' + esc(c.name || '匿名') + '</td>'
        + '<td class="num">' + fmt(c.n) + '</td><td class="num">' + fmt(c.tok) + '</td>'
        + '<td class="num">' + pct(c.ok, c.n) + '</td>'
        + '<td>' + ago(c.last_ts) + '</td></tr>').join(''),
      '还没有令牌产生调用');

    $('recent').innerHTML = simpleTable(
      '<th>时间</th><th>模型</th><th>状态</th><th class="num">tokens</th>'
      + '<th class="num">TTFT</th><th class="num">耗时</th><th>结束原因</th>',
      s.recent.slice(0, 25).map((r) => '<tr><td class="mono">' + when(r.ts) + '</td>'
        + '<td class="mono">' + esc(r.model)
        + (r.stream ? ' <span class="chip acc">流</span>' : '') + '</td>'
        + '<td>' + statusBadge(r.status)
        + (r.attempts > 1 ? ' <span class="chip">重试' + (r.attempts - 1) + '</span>' : '') + '</td>'
        + '<td class="num mono">' + fmt(r.total_tokens) + '</td>'
        + '<td class="num mono">' + ms(r.ttft_ms) + '</td>'
        + '<td class="num mono">' + ms(r.duration_ms) + '</td>'
        + '<td class="muted">' + esc(r.finish_reason || '—') + '</td></tr>').join(''),
      '暂无数据');
  } catch (e) {
    if (e.message !== '未登录') toast('加载失败: ' + e.message, 'bad');
  }
}

document.querySelectorAll('[data-hours]').forEach((btn) => {
  btn.addEventListener('click', () => { location.href = '/?hours=' + btn.dataset.hours; });
});
$('btn-export').addEventListener('click', () => {
  download('/admin/export?hours=' + HOURS, 'mistral_usage_' + HOURS + 'h.csv')
    .catch((e) => toast('导出失败: ' + e.message, 'bad'));
});
window.addEventListener('resize', () => { if (POINTS.length) lineChart($('chart'), POINTS); });

load();
setInterval(load, 5000);
