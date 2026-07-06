// 描画層: 対数-対数チャートのSVGレンダリング(D3利用)
/* global d3 */

const MARGIN = { top: 20, right: 24, bottom: 52, left: 60 };

function isPow10(t) {
  const l = Math.log10(t);
  return Math.abs(l - Math.round(l)) < 1e-9;
}

// 画面座標でのポリゴン重心(ラベル配置用。将来重なり回避に差し替え可能)
function screenCentroid(pts) {
  let area = 0;
  let cx = 0;
  let cy = 0;
  const n = pts.length;
  for (let i = 0; i < n; i++) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[(i + 1) % n];
    const a = x0 * y1 - x1 * y0;
    area += a;
    cx += (x0 + x1) * a;
    cy += (y0 + y1) * a;
  }
  if (Math.abs(area) < 1e-9) {
    return [d3.mean(pts, (p) => p[0]), d3.mean(pts, (p) => p[1])];
  }
  return [cx / (3 * area), cy / (3 * area)];
}

// items: [{ id, label, color, rings: [ [{flow, head}, ...], ... ], meta, selected, dimmed }]
//   rings は多角形の輪郭の配列(通常1本。union外包では飛び地・穴で複数になりうる)
// opts: { width, height, axis: {xmin,xmax,ymin,ymax}(null=自動), onHover(item|null, event), onClick(item|null) }
export function renderChart(svgEl, items, opts) {
  const { width, height } = opts;
  const svg = d3.select(svgEl);
  svg.selectAll('*').remove();
  svg
    .attr('viewBox', `0 0 ${width} ${height}`)
    .attr('width', width)
    .attr('height', height)
    .attr('font-family', 'system-ui, "Hiragino Sans", "Noto Sans JP", sans-serif');
  svg.append('rect').attr('width', width).attr('height', height).attr('fill', '#ffffff');
  svg.on('click', () => opts.onClick?.(null));

  const drawable = items.filter((it) => it.rings.some((r) => r.length >= 2));
  if (drawable.length === 0) return;

  const iw = width - MARGIN.left - MARGIN.right;
  const ih = height - MARGIN.top - MARGIN.bottom;
  const allPts = drawable.flatMap((it) => it.rings.flat());

  const axis = opts.axis ?? {};
  const [dxmin, dxmax] = d3.extent(allPts, (p) => p.flow);
  const [dymin, dymax] = d3.extent(allPts, (p) => p.head);
  const xmin = axis.xmin ?? dxmin * 0.85;
  const xmax = axis.xmax ?? dxmax * 1.18;
  const ymin = axis.ymin ?? dymin * 0.85;
  const ymax = axis.ymax ?? dymax * 1.18;

  const x = d3.scaleLog().domain([xmin, xmax]).range([0, iw]);
  const y = d3.scaleLog().domain([ymin, ymax]).range([ih, 0]);
  const g = svg.append('g').attr('transform', `translate(${MARGIN.left},${MARGIN.top})`);

  // グリッド(対数目盛準拠、10のべき乗は濃く)
  const grid = g.append('g');
  grid
    .selectAll('line.gx')
    .data(x.ticks())
    .join('line')
    .attr('x1', (t) => x(t))
    .attr('x2', (t) => x(t))
    .attr('y1', 0)
    .attr('y2', ih)
    .attr('stroke', (t) => (isPow10(t) ? '#c9ced6' : '#e8ebef'));
  grid
    .selectAll('line.gy')
    .data(y.ticks())
    .join('line')
    .attr('x1', 0)
    .attr('x2', iw)
    .attr('y1', (t) => y(t))
    .attr('y2', (t) => y(t))
    .attr('stroke', (t) => (isPow10(t) ? '#c9ced6' : '#e8ebef'));

  // 軸
  g.append('g')
    .attr('transform', `translate(0,${ih})`)
    .call(d3.axisBottom(x).ticks(8, '~g'));
  g.append('g').call(d3.axisLeft(y).ticks(8, '~g'));
  g.append('text')
    .attr('x', iw / 2)
    .attr('y', ih + 40)
    .attr('text-anchor', 'middle')
    .attr('font-size', 12)
    .attr('fill', '#333')
    .text('吐出し量 Q [m³/min]');
  g.append('text')
    .attr('transform', `translate(${-44},${ih / 2}) rotate(-90)`)
    .attr('text-anchor', 'middle')
    .attr('font-size', 12)
    .attr('fill', '#333')
    .text('全揚程 H [m]');

  // 描画領域クリップ
  svg
    .append('defs')
    .append('clipPath')
    .attr('id', 'plot-clip')
    .append('rect')
    .attr('width', iw)
    .attr('height', ih);

  const line = d3
    .line()
    .x((p) => x(p.flow))
    .y((p) => y(p.head));

  // 選択中を最前面に
  const ordered = [...drawable].sort((a, b) => Number(a.selected) - Number(b.selected));
  const layer = g.append('g').attr('clip-path', 'url(#plot-clip)');
  for (const it of ordered) {
    layer
      .append('path')
      .attr('class', 'polygon')
      .attr('d', it.rings.map((r) => line(r) + 'Z').join(''))
      .attr('fill', it.color)
      .attr('fill-rule', 'evenodd')
      .attr('fill-opacity', it.selected ? 0.55 : 0.3)
      .attr('stroke', it.color)
      .attr('stroke-width', it.selected ? 2.5 : 1.2)
      .attr('stroke-linejoin', 'round')
      .attr('opacity', it.dimmed ? 0.15 : 1)
      .style('cursor', 'pointer')
      .on('mousemove', (ev) => opts.onHover?.(it, ev))
      .on('mouseleave', () => opts.onHover?.(null))
      .on('click', (ev) => {
        ev.stopPropagation();
        opts.onClick?.(it);
      });
  }

  // ラベル(初版はポリゴン重心。FR-2.6)
  const labels = g.append('g').attr('clip-path', 'url(#plot-clip)');
  for (const it of ordered) {
    if (it.dimmed) continue;
    // ラベルは最大リング(頂点数基準)の重心に置く
    const main = it.rings.reduce((a, b) => (b.length > a.length ? b : a));
    const [cx, cy] = screenCentroid(main.map((p) => [x(p.flow), y(p.head)]));
    labels
      .append('text')
      .attr('x', cx)
      .attr('y', cy)
      .attr('text-anchor', 'middle')
      .attr('dominant-baseline', 'middle')
      .attr('font-size', 11)
      .attr('font-weight', it.selected ? 700 : 500)
      .attr('fill', '#37414f')
      .attr('stroke', '#ffffff')
      .attr('stroke-width', 3)
      .attr('paint-order', 'stroke')
      .attr('pointer-events', 'none')
      .text(it.label);
  }
}
