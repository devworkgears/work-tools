import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  interpolateHead,
  clipCurve,
  deriveLower,
  buildPolygon,
  convexHull,
  containsPoint,
  resolveLowerRule,
} from '../src/domain/geometry.js';

const upperA = [
  { flow: 0.1, head: 12 },
  { flow: 0.3, head: 10 },
  { flow: 0.5, head: 7 },
];

function makeModel(over = {}) {
  return { model: 'A', series: 'X', output_kw: 1, diameter_mm: 50, lower_rule: null, lower_param: null, ...over };
}

test('interpolateHead: 線形内挿と範囲外null', () => {
  assert.equal(interpolateHead(upperA, 0.2), 11);
  assert.equal(interpolateHead(upperA, 0.4), 8.5);
  assert.equal(interpolateHead(upperA, 0.1), 12);
  assert.equal(interpolateHead(upperA, 0.05), null);
  assert.equal(interpolateHead(upperA, 0.6), null);
});

test('clipCurve: 端点を内挿して範囲にクリップ', () => {
  const clipped = clipCurve(upperA, 0.2, 0.4);
  assert.deepEqual(clipped, [
    { flow: 0.2, head: 11 },
    { flow: 0.3, head: 10 },
    { flow: 0.4, head: 8.5 },
  ]);
});

test('clipCurve: 重なりなしはnull', () => {
  assert.equal(clipCurve(upperA, 0.6, 0.9), null);
});

test('clip: 水平線が下辺になる(検証項目3)', () => {
  const cbm = { A: { upper: upperA, lower: null } };
  const r = deriveLower(makeModel({ lower_rule: 'clip', lower_param: 4 }), cbm, {});
  assert.equal(r.rule, 'clip');
  assert.deepEqual(r.points, [
    { flow: 0.1, head: 4 },
    { flow: 0.5, head: 4 },
  ]);
});

test('neighbor: 参照upperを借用しクリップ(検証項目2)', () => {
  const refUpper = [
    { flow: 0.2, head: 8 },
    { flow: 0.6, head: 4 },
  ];
  const cbm = { A: { upper: upperA, lower: null }, REF: { upper: refUpper, lower: null } };
  const r = deriveLower(makeModel({ lower_rule: 'neighbor', lower_param: 'REF' }), cbm, {});
  assert.equal(r.error, undefined);
  // 自モデル範囲 [0.1, 0.5] と参照範囲 [0.2, 0.6] の重なり [0.2, 0.5] にクリップ
  assert.equal(r.points[0].flow, 0.2);
  assert.equal(r.points[r.points.length - 1].flow, 0.5);
  assert.ok(Math.abs(r.points[r.points.length - 1].head - 5) < 1e-9);
});

test('neighbor: 参照先なし/範囲非重複はエラー(検証項目5)', () => {
  const cbm = {
    A: { upper: upperA, lower: null },
    FAR: { upper: [{ flow: 2, head: 5 }, { flow: 3, head: 3 }], lower: null },
  };
  const r1 = deriveLower(makeModel({ lower_rule: 'neighbor', lower_param: 'NOPE' }), cbm, {});
  assert.match(r1.error, /NOPE/);
  const r2 = deriveLower(makeModel({ lower_rule: 'neighbor', lower_param: 'FAR' }), cbm, {});
  assert.match(r2.error, /重なりません/);
});

test('ratio: 揚程をk倍。kの範囲チェックあり', () => {
  const cbm = { A: { upper: upperA, lower: null } };
  const r = deriveLower(makeModel({ lower_rule: 'ratio', lower_param: 0.5 }), cbm, {});
  assert.deepEqual(r.points.map((p) => p.head), [6, 5, 3.5]);
  const bad = deriveLower(makeModel({ lower_rule: 'ratio', lower_param: 1.5 }), cbm, {});
  assert.ok(bad.error);
});

test('trim: 相似則(Q×r, H×r²)で導出し自範囲にクリップ', () => {
  const r = 0.8;
  const cbm = { A: { upper: upperA, lower: null } };
  const res = deriveLower(makeModel({ lower_rule: 'trim', lower_param: r }), cbm, {});
  assert.equal(res.error, undefined);
  // トリム後の範囲 [0.08, 0.4] → 自範囲 [0.1, 0.5] とのクリップで [0.1, 0.4]
  assert.ok(Math.abs(res.points[0].flow - 0.1) < 1e-9);
  assert.ok(Math.abs(res.points[res.points.length - 1].flow - 0.4) < 1e-9);
  // 右端はトリム曲線の終点 (0.5*0.8, 7*0.64)
  assert.ok(Math.abs(res.points[res.points.length - 1].head - 7 * 0.64) < 1e-9);
});

test('explicit: lower点列があればルール指定より優先(検証項目4)', () => {
  const lower = [
    { flow: 0.1, head: 5 },
    { flow: 0.5, head: 3 },
  ];
  const cbm = { A: { upper: upperA, lower } };
  const model = makeModel({ lower_rule: 'clip', lower_param: 4 });
  assert.equal(resolveLowerRule(model, cbm.A, {}).rule, 'explicit');
  const r = deriveLower(model, cbm, {});
  assert.equal(r.rule, 'explicit');
  assert.deepEqual(r.points, lower);
});

test('ルール未指定はconfig既定にフォールバック。両方なしはエラー', () => {
  const cbm = { A: { upper: upperA, lower: null } };
  const ok = deriveLower(makeModel(), cbm, { default_lower_rule: 'clip', default_lower_param: 2 });
  assert.equal(ok.rule, 'clip');
  assert.equal(ok.points[0].head, 2);
  const ng = deriveLower(makeModel(), cbm, {});
  assert.match(ng.error, /未指定/);
});

test('buildPolygon: upper昇順→lower降順の閉路(§3.3)', () => {
  const lower = [
    { flow: 0.1, head: 4 },
    { flow: 0.5, head: 4 },
  ];
  const poly = buildPolygon(upperA, lower);
  assert.equal(poly.length, 5);
  assert.deepEqual(poly[0], { flow: 0.1, head: 12 });
  assert.deepEqual(poly[2], { flow: 0.5, head: 7 }); // upper右端
  assert.deepEqual(poly[3], { flow: 0.5, head: 4 }); // lower右端(右辺接続)
  assert.deepEqual(poly[4], { flow: 0.1, head: 4 }); // lower左端(左辺接続)
});

test('convexHull: 内部点は除去され4隅が残る', () => {
  const hull = convexHull([[0, 0], [1, 0], [1, 1], [0, 1], [0.5, 0.5]]);
  assert.equal(hull.length, 4);
  const set = new Set(hull.map((p) => p.join(',')));
  assert.ok(set.has('0,0') && set.has('1,0') && set.has('1,1') && set.has('0,1'));
});

test('containsPoint: 内・外・境界上(検証項目10)', () => {
  const poly = [
    { flow: 0.1, head: 12 },
    { flow: 0.5, head: 7 },
    { flow: 0.5, head: 4 },
    { flow: 0.1, head: 4 },
  ];
  assert.equal(containsPoint(poly, 0.3, 6), true); // 領域内
  assert.equal(containsPoint(poly, 0.3, 12), false); // 領域外(上)
  assert.equal(containsPoint(poly, 0.05, 5), false); // 領域外(左)
  assert.equal(containsPoint(poly, 0.5, 5), true); // 右辺上
  assert.equal(containsPoint(poly, 0.1, 4), true); // 頂点上
  assert.equal(containsPoint(poly, 0.3, 4), true); // 下辺上
});
