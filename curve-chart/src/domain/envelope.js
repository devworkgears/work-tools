// グループ外包の境界計算(FR-3.2)。方式: 凸包(hull) / 合併境界(union)
// チャートは対数-対数軸で点を直線接続するため、幾何計算は対数空間で行い結果を戻す。
// union は同梱の polygon-clipping(martinez法)を使用(src/vendor/)。

import { convexHull } from './geometry.js';
import polygonClipping from '../vendor/polygon-clipping.mjs';

export const ENVELOPE_METHODS = ['hull', 'union'];

const toLog = (ring) => ring.map((p) => [Math.log(p.flow), Math.log(p.head)]);
const fromLog = (ring) => ring.map(([lx, ly]) => ({ flow: Math.exp(lx), head: Math.exp(ly) }));

// polygons: リング配列の配列(各要素が1モデルの運転範囲ポリゴン [{flow, head}, ...])
// 戻り値: リング配列(hullは常に1本、unionは飛び地・穴で複数になりうる)
export function buildEnvelope(polygons, method = 'hull') {
  const rings = polygons.filter((r) => r && r.length >= 3);
  if (rings.length === 0) return [];

  if (method === 'hull') {
    return [fromLog(convexHull(rings.flatMap(toLog)))];
  }

  if (method === 'union') {
    // MultiPolygon形式([ [ring, hole...], ... ])で一括union
    const result = polygonClipping.union(...rings.map((r) => [[toLog(r)]]));
    const out = [];
    for (const poly of result) {
      for (const ring of poly) {
        // polygon-clippingは始点=終点で閉じるので末尾を除く
        const closed = ring.length >= 2 && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1];
        out.push(fromLog(closed ? ring.slice(0, -1) : ring));
      }
    }
    return out;
  }

  throw new Error(`未対応の外包方式です: ${method}`);
}
