// 幾何計算(UI非依存の純粋関数)
// - lower導出ルール(explicit / neighbor / clip / ratio / trim)
// - ポリゴン生成・凸包・点の内外判定(内外判定は選定システム用の先行実装)

import { isBlank, toNumber } from './parser.js';

const EPS = 1e-9;

// flow昇順の点列に対する線形内挿。範囲外は null
export function interpolateHead(points, flow) {
  if (!points || points.length === 0) return null;
  if (flow < points[0].flow - EPS || flow > points[points.length - 1].flow + EPS) return null;
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i];
    const b = points[i + 1];
    if (flow <= b.flow + EPS) {
      if (Math.abs(b.flow - a.flow) < EPS) return a.head;
      const t = (flow - a.flow) / (b.flow - a.flow);
      return a.head + t * (b.head - a.head);
    }
  }
  return points[points.length - 1].head;
}

// 点列を流量範囲 [fmin, fmax] に内挿クリップ。重なりがなければ null
export function clipCurve(points, fmin, fmax) {
  if (!points || points.length < 2) return null;
  const start = Math.max(fmin, points[0].flow);
  const end = Math.min(fmax, points[points.length - 1].flow);
  if (start > end + EPS) return null;
  const out = [{ flow: start, head: interpolateHead(points, start) }];
  for (const p of points) {
    if (p.flow > start + EPS && p.flow < end - EPS) out.push({ flow: p.flow, head: p.head });
  }
  if (end > start + EPS) out.push({ flow: end, head: interpolateHead(points, end) });
  return out;
}

// 適用ルールの解決: explicit(lower点列あり)が常に最優先 > モデル個別指定 > config既定
export function resolveLowerRule(model, entry, config) {
  if (entry?.lower && entry.lower.length >= 2) return { rule: 'explicit', param: null };
  if (model.lower_rule) return { rule: model.lower_rule, param: model.lower_param };
  const rule = isBlank(config?.default_lower_rule) ? null : String(config.default_lower_rule).trim();
  return { rule, param: config?.default_lower_param ?? null };
}

// §3.3 の導出ルール。戻り値: { rule, points } または { rule, error }
export function deriveLower(model, curvesByModel, config) {
  const entry = curvesByModel[model.model];
  const upper = entry?.upper;
  if (!upper || upper.length < 2) return { rule: null, error: 'upperカーブがありません' };
  const fmin = upper[0].flow;
  const fmax = upper[upper.length - 1].flow;
  const { rule, param } = resolveLowerRule(model, entry, config);
  const fail = (error) => ({ rule, error });
  if (!rule) return fail('lower_rule が未指定で、configに既定ルール(default_lower_rule)もありません');

  switch (rule) {
    case 'explicit':
      return { rule, points: entry.lower.map(({ flow, head }) => ({ flow, head })) };

    case 'neighbor': {
      if (isBlank(param)) return fail('neighbor には参照モデル名(lower_param)が必要です');
      const refName = String(param).trim();
      if (refName === model.model) return fail('neighbor の参照先が自分自身です');
      const ref = curvesByModel[refName]?.upper;
      if (!ref || ref.length < 2) return fail(`参照モデル「${refName}」のupperカーブが見つかりません`);
      const pts = clipCurve(ref, fmin, fmax);
      if (!pts) return fail(`参照モデル「${refName}」と流量範囲が重なりません`);
      return { rule, points: pts };
    }

    case 'clip': {
      const h = toNumber(param);
      if (h === null || h <= 0) return fail(`clip には正の下限揚程[m]が必要です: ${param}`);
      return { rule, points: [{ flow: fmin, head: h }, { flow: fmax, head: h }] };
    }

    case 'ratio': {
      const k = toNumber(param);
      if (k === null || k <= 0 || k >= 1) return fail(`ratio の係数 k は 0<k<1 が必要です: ${param}`);
      return { rule, points: upper.map((p) => ({ flow: p.flow, head: p.head * k })) };
    }

    case 'trim': {
      const r = toNumber(param);
      if (r === null || r <= 0 || r >= 1) return fail(`trim のトリム比 r は 0<r<1 が必要です: ${param}`);
      const trimmed = upper.map((p) => ({ flow: p.flow * r, head: p.head * r * r }));
      const pts = clipCurve(trimmed, fmin, fmax);
      if (!pts) return fail('トリム後のカーブが自モデルの流量範囲と重なりません');
      return { rule, points: pts };
    }

    default:
      return fail(`未対応の lower_rule です: ${rule}`);
  }
}

// §3.3 ポリゴン生成規則: upperを昇順→右辺→lowerを降順→左辺(閉路)
export function buildPolygon(upper, lower) {
  const u = upper.map(({ flow, head }) => ({ flow, head }));
  const l = lower.map(({ flow, head }) => ({ flow, head })).reverse();
  return [...u, ...l];
}

// 凸包(monotone chain)。点は [x, y] の配列。対数軸上の外包に使う場合は呼び出し側で対数変換する
export function convexHull(points) {
  const sorted = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const uniq = sorted.filter(
    (p, i) => i === 0 || Math.abs(p[0] - sorted[i - 1][0]) > EPS || Math.abs(p[1] - sorted[i - 1][1]) > EPS
  );
  if (uniq.length <= 2) return uniq;
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [];
  for (const p of uniq) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
    lower.push(p);
  }
  const upper = [];
  for (let i = uniq.length - 1; i >= 0; i--) {
    const p = uniq[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
    upper.push(p);
  }
  return [...lower.slice(0, -1), ...upper.slice(0, -1)];
}

function onSegment(a, b, q, h, tol) {
  const dx = b.flow - a.flow;
  const dy = b.head - a.head;
  const len = Math.hypot(dx, dy);
  if (len < EPS) return Math.hypot(q - a.flow, h - a.head) <= tol;
  const cross = dx * (h - a.head) - dy * (q - a.flow);
  if (Math.abs(cross) / len > tol) return false;
  const dot = (q - a.flow) * dx + (h - a.head) * dy;
  return dot >= -tol * len && dot <= len * len + tol * len;
}

// 点の内外判定(ray casting)。境界上は「内」とみなす。選定システム用の先行実装(§4.2)
export function containsPoint(polygon, q, h, tol = 1e-9) {
  const n = polygon.length;
  if (n < 3) return false;
  for (let i = 0; i < n; i++) {
    if (onSegment(polygon[i], polygon[(i + 1) % n], q, h, tol)) return true;
  }
  let inside = false;
  for (let i = 0, j = n - 1; i < n; j = i++) {
    const yi = polygon[i].head;
    const yj = polygon[j].head;
    if ((yi > h) !== (yj > h)) {
      const xint = polygon[i].flow + ((h - yi) / (yj - yi)) * (polygon[j].flow - polygon[i].flow);
      if (q < xint) inside = !inside;
    }
  }
  return inside;
}
