import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildEnvelope } from '../src/domain/envelope.js';
import { containsPoint } from '../src/domain/geometry.js';

// 軸平行の矩形リングを作るヘルパ(flow/head正値)
const rect = (f0, f1, h0, h1) => [
  { flow: f0, head: h0 },
  { flow: f1, head: h0 },
  { flow: f1, head: h1 },
  { flow: f0, head: h1 },
];

test('hull: 全ポリゴンを包含する単一リング(検証項目8)', () => {
  const polys = [rect(0.1, 0.3, 5, 10), rect(0.2, 0.5, 8, 20)];
  const rings = buildEnvelope(polys, 'hull');
  assert.equal(rings.length, 1);
  // 元の全頂点が凸包の内側または境界上にある
  for (const p of polys.flat()) {
    assert.ok(containsPoint(rings[0], p.flow, p.head, 1e-6), `(${p.flow}, ${p.head}) が外包の外`);
  }
});

test('union: 重なる2矩形は1リングに合併され、凹部は埋めない', () => {
  const rings = buildEnvelope([rect(0.1, 0.3, 5, 10), rect(0.2, 0.5, 8, 20)], 'union');
  assert.equal(rings.length, 1);
  const ring = rings[0];
  // L字型の凹部(f=0.4, h=6 は矩形2の下・矩形1の右外)は含まれない
  assert.equal(containsPoint(ring, 0.4, 6), false);
  // 両矩形の内部点は含まれる
  assert.equal(containsPoint(ring, 0.15, 7), true);
  assert.equal(containsPoint(ring, 0.45, 15), true);
  // hullなら凹部が埋まる(方式の違いの確認)
  const hull = buildEnvelope([rect(0.1, 0.3, 5, 10), rect(0.2, 0.5, 8, 20)], 'hull')[0];
  assert.equal(containsPoint(hull, 0.4, 6.5), true);
});

test('union: 離れた2矩形は2リング(飛び地)になる', () => {
  const rings = buildEnvelope([rect(0.1, 0.2, 5, 10), rect(1, 2, 30, 50)], 'union');
  assert.equal(rings.length, 2);
});

test('リングが3点未満のポリゴンは無視、全滅なら空配列', () => {
  assert.deepEqual(buildEnvelope([], 'union'), []);
  assert.deepEqual(buildEnvelope([[{ flow: 1, head: 1 }, { flow: 2, head: 2 }]], 'hull'), []);
});

test('未知の方式は例外', () => {
  assert.throws(() => buildEnvelope([rect(0.1, 0.2, 5, 10)], 'magic'), /未対応/);
});
