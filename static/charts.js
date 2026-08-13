/* 手写 SVG 图表，不引第三方库。 */

/**
 * 主折线图。points: [{ value, error, label, rows: [[色, 名, 值], …] }]
 * tooltip 用自定义浮层而不是 <title>：原生 title 有约 1 秒延迟，样式也没法控。
 */
function lineChart(el, points) {
  if (!points.length) {
    el.innerHTML = '<div class="empty-state">' + ic('chart', 28)
      + '<p>该时间范围内没有数据</p></div>';
    return;
  }
  const W = el.clientWidth || 900;
  const H = 250;
  const padL = 52, padR = 18, padT = 18, padB = 30;
  const iw = Math.max(10, W - padL - padR);
  const ih = H - padT - padB;

  const maxY = niceMax(Math.max(1, ...points.map((p) => p.value)));
  const maxErr = Math.max(1, ...points.map((p) => p.error || 0));
  const n = points.length;
  const stepX = n > 1 ? iw / (n - 1) : 0;
  const x = (i) => padL + (n > 1 ? i * stepX : iw / 2);
  const y = (v) => padT + ih - (v / maxY) * ih;

  const linePts = points.map((p, i) => x(i) + ',' + y(p.value)).join(' ');
  const areaPts = padL + ',' + (padT + ih) + ' ' + linePts + ' ' + x(n - 1) + ',' + (padT + ih);

  const grid = [0, .25, .5, .75, 1].map((f) => {
    const yy = padT + ih - f * ih;
    return '<line class="grid" x1="' + padL + '" y1="' + yy + '" x2="' + (W - padR) + '" y2="' + yy + '"/>'
      + '<text class="tick" text-anchor="end" x="' + (padL - 10) + '" y="' + (yy + 3.5) + '">'
      + fmt(Math.round(maxY * f)) + '</text>';
  }).join('');

  const barW = Math.max(2, Math.min(11, iw / n * 0.42));
  const bars = points.map((p, i) => {
    if (!p.error) return '';
    const h = Math.max(3, (p.error / maxErr) * ih * 0.35);
    return '<rect class="errbar" x="' + (x(i) - barW / 2) + '" y="' + (padT + ih - h)
      + '" width="' + barW + '" height="' + h + '" rx="1.5"/>';
  }).join('');

  const every = Math.max(1, Math.ceil(n / 9));
  let ticks = '';
  for (let i = 0; i < n; i += every) {
    ticks += '<text class="tick" text-anchor="middle" x="' + x(i) + '" y="' + (H - 9) + '">'
      + esc(points[i].label) + '</text>';
  }

  // 点少的时候把数据点画出来，否则几段折线看着像没画完
  const dots = n <= 40
    ? points.map((p, i) => '<circle cx="' + x(i) + '" cy="' + y(p.value)
        + '" r="3" fill="var(--accent)" stroke="var(--surface)" stroke-width="2"/>').join('')
    : '';

  el.innerHTML = '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" height="' + H + '">'
    + '<defs><linearGradient id="chartFill" x1="0" y1="0" x2="0" y2="1">'
    + '<stop offset="0%" stop-color="var(--accent)" stop-opacity=".34"/>'
    + '<stop offset="100%" stop-color="var(--accent)" stop-opacity=".02"/></linearGradient></defs>'
    + grid + bars
    + '<polygon class="fill" points="' + areaPts + '"/>'
    + '<polyline class="line" points="' + linePts + '"/>'
    + dots + ticks
    + '<g class="cursor"><line y1="' + padT + '" y2="' + (padT + ih) + '"/><circle r="4.5"/></g>'
    + '<rect class="hit" x="' + padL + '" y="' + padT + '" width="' + iw + '" height="' + ih + '"/>'
    + '</svg><div class="chart-tip"></div>';

  const svg = el.querySelector('svg');
  const cursor = el.querySelector('.cursor');
  const tip = el.querySelector('.chart-tip');
  const hit = el.querySelector('.hit');

  hit.addEventListener('mousemove', (ev) => {
    const box = svg.getBoundingClientRect();
    const scale = W / box.width;
    const px = (ev.clientX - box.left) * scale;
    const i = Math.max(0, Math.min(n - 1, Math.round((px - padL) / (stepX || 1))));
    const p = points[i];

    cursor.classList.add('on');
    cursor.querySelector('line').setAttribute('x1', x(i));
    cursor.querySelector('line').setAttribute('x2', x(i));
    cursor.querySelector('circle').setAttribute('cx', x(i));
    cursor.querySelector('circle').setAttribute('cy', y(p.value));

    tip.innerHTML = '<b>' + esc(p.title) + '</b>'
      + p.rows.map((r) => '<div class="row"><i style="background:' + r[0] + '"></i>'
        + esc(r[1]) + '<span>' + r[2] + '</span></div>').join('');
    tip.classList.add('on');
    const left = padL + (x(i) - padL) / scale + (box.left - el.getBoundingClientRect().left);
    tip.style.left = Math.round(x(i) / scale) + 'px';
    tip.style.top = Math.round(y(p.value) / scale + 4) + 'px';
  });
  hit.addEventListener('mouseleave', () => {
    cursor.classList.remove('on');
    tip.classList.remove('on');
  });
}

/* 坐标轴上限取整成好读的数。档位给密一些（含 1.5/3/4/8），
   否则峰值 250 会被抬到 500，图形只占一半高度。 */
function niceMax(v) {
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const base = v / exp;
  const step = base <= 1 ? 1 : base <= 1.5 ? 1.5 : base <= 2 ? 2 : base <= 2.5 ? 2.5
    : base <= 3 ? 3 : base <= 4 ? 4 : base <= 5 ? 5 : base <= 6 ? 6 : base <= 8 ? 8 : 10;
  return step * exp;
}

/* 主指标卡里的迷你趋势线 */
function sparkline(values, color, w, h) {
  w = w || 88; h = h || 26;
  if (!values || values.length < 2) return '';
  const max = Math.max(...values), min = Math.min(...values);
  const span = max - min || 1;
  const step = w / (values.length - 1);
  const pts = values.map((v, i) => (i * step).toFixed(1) + ','
    + (h - 2 - ((v - min) / span) * (h - 4)).toFixed(1)).join(' ');
  return '<svg class="spark" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">'
    + '<polyline class="spark-line" points="' + pts + '" stroke="' + color + '"/></svg>';
}

/* 横向占比条列表，用于状态码/端点分布 */
function barList(rows) {
  if (!rows.length) return '<div class="empty-state"><p>暂无数据</p></div>';
  const max = Math.max(1, ...rows.map((r) => r.value));
  return '<table><tbody>' + rows.map((r) => {
    const width = Math.round(r.value / max * 100);
    return '<tr><td style="width:1%;border:0;padding:6px 10px 6px 0">' + r.badge + '</td>'
      + '<td style="border:0;padding:6px 0"><div class="bar"><i class="' + (r.tone || '')
      + '" style="width:' + width + '%"></i></div></td>'
      + '<td class="num mono" style="width:1%;border:0;padding:6px 0 6px 12px">' + fmt(r.value) + '</td>'
      + '<td class="num muted" style="width:1%;border:0;padding:6px 0 6px 10px">' + r.share + '</td></tr>';
  }).join('') + '</tbody></table>';
}
