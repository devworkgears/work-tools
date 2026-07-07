// 実図デジタイザ(v0.3 実データ検証支援)の座標計算 — 純粋関数
// 実図の軸種別(対数/リニア)ごとに、画像ピクセル↔実座標(Q,H)を「変換後空間の線形写像」で結ぶ。
//   対数軸:   log(v) = a*p + b
//   リニア軸:      v = a*p + b
// 注意: ツールのチャートは常に両対数のため、リニア軸の実図は角位置しか正確に表示できない
// (画像は歪んで見える)。ただし採取クリックの座標変換はチャート軸で行うため採取精度には影響しない。

// 画像の論理ピクセル空間。実画像の寸法とは独立(preserveAspectRatio=noneで矩形に引き伸ばして
// 表示するため実寸は不要)。較正前の仮表示はアスペクト比が狂いうるが、較正後は軸に整合する。
export const IMAGE_SPACE = { width: 1000, height: 750 };

export const AXIS_SCALES = ['log', 'linear'];

const T = {
  log: { fwd: Math.log, inv: Math.exp, valid: (v) => Number.isFinite(v) && v > 0 },
  linear: { fwd: (v) => v, inv: (v) => v, valid: Number.isFinite },
};

// 2つの基準点から写像を作る。scales = { q: 'log'|'linear', h: 'log'|'linear' }(省略時は両対数)
// p = { px, py, q, h }。無効な入力(同一点・対数軸の非正値)は null
export function calibrate(p1, p2, scales = {}) {
  const qT = T[scales.q ?? 'log'];
  const hT = T[scales.h ?? 'log'];
  if (!p1 || !p2 || !qT || !hT) return null;
  if (!qT.valid(p1.q) || !qT.valid(p2.q) || !hT.valid(p1.h) || !hT.valid(p2.h)) return null;
  if (p1.px === p2.px || p1.py === p2.py || p1.q === p2.q || p1.h === p2.h) return null;
  const ax = (qT.fwd(p2.q) - qT.fwd(p1.q)) / (p2.px - p1.px);
  const ay = (hT.fwd(p2.h) - hT.fwd(p1.h)) / (p2.py - p1.py);
  return {
    q: scales.q ?? 'log',
    h: scales.h ?? 'log',
    ax,
    bx: qT.fwd(p1.q) - ax * p1.px,
    ay,
    by: hT.fwd(p1.h) - ay * p1.py,
  };
}

export function imageToData(m, px, py) {
  return {
    flow: T[m.q ?? 'log'].inv(m.ax * px + m.bx),
    head: T[m.h ?? 'log'].inv(m.ay * py + m.by),
  };
}

export function dataToImage(m, flow, head) {
  return {
    px: (T[m.q ?? 'log'].fwd(flow) - m.bx) / m.ax,
    py: (T[m.h ?? 'log'].fwd(head) - m.by) / m.ay,
  };
}

// 較正前の仮写像: 画像全体を指定のQ-H範囲(通常は現在の軸範囲)に張る。画像上端=高H側。
// チャート自体が両対数なので仮写像は常に対数
export function fitMapping(domain) {
  return calibrate(
    { px: 0, py: IMAGE_SPACE.height, q: domain.xmin, h: domain.ymin },
    { px: IMAGE_SPACE.width, py: 0, q: domain.xmax, h: domain.ymax }
  );
}

// 採取点 [{model, curve, flow, head}] → Excel書き出し用の行(テンプレ互換の models / curves)
export function buildExportRows(points) {
  const curves = [...points]
    .sort((a, b) => a.model.localeCompare(b.model) || a.curve.localeCompare(b.curve) || a.flow - b.flow)
    .map((p) => ({ model: p.model, curve: p.curve, flow: p.flow, head: p.head }));
  const models = [...new Set(points.map((p) => p.model))].sort((a, b) => a.localeCompare(b)).map((model) => ({
    model,
    series: null,
    output_kw: null,
    diameter_mm: null,
    lower_rule: null,
    lower_param: null,
  }));
  return { models, curves };
}
