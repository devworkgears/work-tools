// 実図デジタイザ(v0.3 実データ検証支援)の座標計算 — 純粋関数
// 前提: 実図は両対数チャート。画像ピクセル↔実座標(Q,H)は対数空間の線形写像で結ばれる。

// 画像の論理ピクセル空間。実画像の寸法とは独立(preserveAspectRatio=noneで矩形に引き伸ばして
// 表示するため実寸は不要)。較正前の仮表示はアスペクト比が狂いうるが、較正後は軸に正しく整合する。
export const IMAGE_SPACE = { width: 1000, height: 750 };

// 2つの基準点から写像係数 {ax, bx, ay, by} を求める。log(q) = ax*px + bx, log(h) = ay*py + by
// p = { px, py, q, h }。無効な入力(同一点・非正値)は null
export function calibrate(p1, p2) {
  if (!p1 || !p2) return null;
  if (!(p1.q > 0) || !(p2.q > 0) || !(p1.h > 0) || !(p2.h > 0)) return null;
  if (p1.px === p2.px || p1.py === p2.py || p1.q === p2.q || p1.h === p2.h) return null;
  const ax = (Math.log(p2.q) - Math.log(p1.q)) / (p2.px - p1.px);
  const ay = (Math.log(p2.h) - Math.log(p1.h)) / (p2.py - p1.py);
  return { ax, bx: Math.log(p1.q) - ax * p1.px, ay, by: Math.log(p1.h) - ay * p1.py };
}

export function imageToData(m, px, py) {
  return { flow: Math.exp(m.ax * px + m.bx), head: Math.exp(m.ay * py + m.by) };
}

export function dataToImage(m, flow, head) {
  return { px: (Math.log(flow) - m.bx) / m.ax, py: (Math.log(head) - m.by) / m.ay };
}

// 較正前の仮写像: 画像全体を指定のQ-H範囲(通常は現在の軸範囲)に張る。画像上端=高H側
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
