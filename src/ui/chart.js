// 描画層: 対数-対数チャートのSVGレンダリング(D3利用)
/* global d3 */
import { imageToData, IMAGE_SPACE } from './digitizer.js';

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

// ラベル重なり回避(FR-2.6): 中心座標のボックス群を上下に押し出して分離する純粋関数。
// アルゴリズム差し替え可能な境界として公開(テストもこの単位で行う)
export function separateLabels(boxes, width, height, { pad = 2, maxIter = 50 } = {}) {
  for (let iter = 0; iter < maxIter; iter++) {
    let moved = false;
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i];
        const b = boxes[j];
        const ox = (a.w + b.w) / 2 + pad - Math.abs(a.x - b.x);
        const oy = (a.h + b.h) / 2 + pad - Math.abs(a.y - b.y);
        if (ox > 0 && oy > 0) {
          const dir = a.y <= b.y ? 1 : -1;
          a.y -= (dir * oy) / 2;
          b.y += (dir * oy) / 2;
          moved = true;
        }
      }
    }
    if (!moved) break;
  }
  for (const b of boxes) {
    b.x = Math.min(Math.max(b.x, b.w / 2), width - b.w / 2);
    b.y = Math.min(Math.max(b.y, b.h / 2), height - b.h / 2);
  }
  return boxes;
}

// ラベル幅の概算(11pxフォント。全角はほぼ正方、半角は約0.6文字分)
function estimateLabelWidth(text) {
  let w = 6;
  for (const ch of String(text)) w += ch.codePointAt(0) > 0xff ? 11 : 6.6;
  return w;
}

// items: [{ id, label, color, rings: [ [{flow, head}, ...], ... ], meta, selected, dimmed }]
//   rings は多角形の輪郭の配列(通常1本。union外包では飛び地・穴で複数になりうる)
// opts: { width, height, axis: {xmin,xmax,ymin,ymax}(null=自動),
//         underlay: {href, mapping, opacity}(実図デジタイザの背景画像),
//         captureMode: bool(クリックで点採取。選択操作は無効化),
//         onHover(item|null, event), onClick(item|null), onMove(q|null, h|null), onPlotClick(q, h) }
export function renderChart(svgEl, items, opts) {
  const { width, height } = opts;
  const svg = d3.select(svgEl);
  svg.selectAll('*').remove();
  svg
    .attr('viewBox', `0 0 ${width} ${height}`)
    .attr('width', width)
    .attr('height', height)
    .attr('font-family', 'system-ui, "Hiragino Sans", "Noto Sans JP", sans-serif')
    .style('cursor', opts.captureMode ? 'crosshair' : null);
  svg.append('rect').attr('width', width).attr('height', height).attr('fill', '#ffffff');

  const drawable = items.filter((it) => it.rings.some((r) => r.length >= 2));
  // データなしでも背景画像(デジタイザ)があれば軸を出して作業できるようにする
  if (drawable.length === 0 && !opts.underlay?.href) {
    svg.on('click', () => opts.onClick?.(null));
    return;
  }

  const iw = width - MARGIN.left - MARGIN.right;
  const ih = height - MARGIN.top - MARGIN.bottom;
  const allPts = drawable.flatMap((it) => it.rings.flat());

  const axis = opts.axis ?? {};
  let [dxmin, dxmax] = [0.05, 5];
  let [dymin, dymax] = [1, 100];
  if (allPts.length > 0) {
    [dxmin, dxmax] = d3.extent(allPts, (p) => p.flow);
    [dymin, dymax] = d3.extent(allPts, (p) => p.head);
    dxmin *= 0.85;
    dxmax *= 1.18;
    dymin *= 0.85;
    dymax *= 1.18;
  }
  const xmin = axis.xmin ?? dxmin;
  const xmax = axis.xmax ?? dxmax;
  const ymin = axis.ymin ?? dymin;
  const ymax = axis.ymax ?? dymax;

  // svg左上原点からのプロット座標(getScreenCTM非依存: jsdom・実ブラウザ両対応)
  const plotCoords = (ev) => {
    const r = svgEl.getBoundingClientRect();
    return [ev.clientX - r.left - MARGIN.left, ev.clientY - r.top - MARGIN.top];
  };
  svg.on('click', (ev) => {
    if (opts.captureMode) {
      const [mx, my] = plotCoords(ev);
      if (mx >= 0 && mx <= iw && my >= 0 && my <= ih) opts.onPlotClick?.(x.invert(mx), y.invert(my));
      return;
    }
    opts.onClick?.(null);
  });

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

  // 実図の背景画像(グリッドの上・軸とポリゴンの下)
  if (opts.underlay?.href && opts.underlay.mapping) {
    const m = opts.underlay.mapping;
    const c0 = imageToData(m, 0, 0);
    const c1 = imageToData(m, IMAGE_SPACE.width, IMAGE_SPACE.height);
    // リニア軸較正の実図では画像の角が非正の座標になりうるため、対数スケールに
    // 載る範囲へクランプする(リニア実図の表示はもとより近似。採取精度には無関係)
    const cq = (v) => Math.min(Math.max(v, xmin / 100), xmax * 100);
    const ch = (v) => Math.min(Math.max(v, ymin / 100), ymax * 100);
    const xs = [x(cq(c0.flow)), x(cq(c1.flow))];
    const ys = [y(ch(c0.head)), y(ch(c1.head))];
    g.append('image')
      .attr('href', opts.underlay.href)
      .attr('x', Math.min(...xs))
      .attr('y', Math.min(...ys))
      .attr('width', Math.abs(xs[1] - xs[0]))
      .attr('height', Math.abs(ys[1] - ys[0]))
      .attr('preserveAspectRatio', 'none')
      .attr('opacity', opts.underlay.opacity ?? 0.5)
      .attr('clip-path', 'url(#plot-clip)')
      .attr('pointer-events', 'none');
  }

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
      .attr('pointer-events', opts.captureMode ? 'none' : null)
      .style('cursor', 'pointer')
      .on('mousemove', (ev) => opts.onHover?.(it, ev))
      .on('mouseleave', () => opts.onHover?.(null))
      .on('click', (ev) => {
        ev.stopPropagation();
        opts.onClick?.(it);
      });
  }

  // ラベル: ポリゴン重心に置き、重なりを押し出しで回避(FR-2.6)
  const labelItems = ordered.filter((it) => !it.dimmed);
  const boxes = labelItems.map((it) => {
    // ラベルは最大リング(頂点数基準)の重心に置く
    const main = it.rings.reduce((a, b) => (b.length > a.length ? b : a));
    const [cx, cy] = screenCentroid(main.map((p) => [x(p.flow), y(p.head)]));
    return { x: cx, y: cy, w: estimateLabelWidth(it.label), h: 14 };
  });
  separateLabels(boxes, iw, ih);
  const labels = g.append('g').attr('clip-path', 'url(#plot-clip)');
  labelItems.forEach((it, i) => {
    labels
      .append('text')
      .attr('x', boxes[i].x)
      .attr('y', boxes[i].y)
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
  });

  // カーソル位置のQ-H読み取り
  svg.on('mousemove', (ev) => {
    const [mx, my] = plotCoords(ev);
    if (mx >= 0 && mx <= iw && my >= 0 && my <= ih) opts.onMove?.(x.invert(mx), y.invert(my));
    else opts.onMove?.(null, null);
  });
  svg.on('mouseleave', () => opts.onMove?.(null, null));
}
