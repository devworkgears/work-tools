// サンプルデータ全体を通しで処理する統合テスト(parse → lower導出 → ポリゴン生成 → 内外判定)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseDataset } from '../src/domain/parser.js';
import { deriveLower, buildPolygon, containsPoint } from '../src/domain/geometry.js';
import { applyFilters } from '../src/domain/query.js';
import { sampleSheets } from '../src/sample-data.js';

function buildAll() {
  const ds = parseDataset(sampleSheets());
  const polygons = new Map();
  const errors = [...ds.errors];
  for (const m of ds.models) {
    const entry = ds.curvesByModel[m.model];
    const lower = deriveLower(m, ds.curvesByModel, ds.config);
    if (lower.error) {
      errors.push({ model: m.model, message: lower.error });
      continue;
    }
    polygons.set(m.model, { points: buildPolygon(entry.upper, lower.points), rule: lower.rule });
  }
  return { ds, polygons, errors };
}

test('サンプル10モデル全てがエラーなくポリゴン化される(検証項目1相当)', () => {
  const { polygons, errors } = buildAll();
  assert.equal(errors.length, 0, JSON.stringify(errors, null, 2));
  assert.equal(polygons.size, 10);
  for (const [name, { points }] of polygons) {
    assert.ok(points.length >= 4, `${name}: 頂点数不足 (${points.length})`);
    for (const p of points) assert.ok(p.flow > 0 && p.head > 0, `${name}: 非正の座標`);
  }
});

test('適用ルールが期待どおり解決される', () => {
  const { polygons } = buildAll();
  assert.equal(polygons.get('PA-1.5').rule, 'clip');
  assert.equal(polygons.get('PA-5.5').rule, 'neighbor');
  assert.equal(polygons.get('PB-3.7').rule, 'clip'); // config既定にフォールバック
  assert.equal(polygons.get('PC-1.5').rule, 'explicit'); // lower点列あり
  assert.equal(polygons.get('PC-2.2').rule, 'ratio');
  assert.equal(polygons.get('PC-3.7').rule, 'trim');
});

test('neighborの下辺は参照モデルのupperと一致する(借用)', () => {
  const { ds, polygons } = buildAll();
  const poly = polygons.get('PA-2.2').points;
  const refUpper = ds.curvesByModel['PA-1.5'].upper;
  // 両モデルの流量範囲は同一なので、下辺(後半・降順)は参照upperの逆順と一致する
  const lowerPart = poly.slice(5).reverse();
  assert.deepEqual(
    lowerPart.map((p) => [p.flow, p.head]),
    refUpper.map((p) => [p.flow, p.head])
  );
});

test('運転範囲の中心付近は内、範囲外は外(containsPoint実用確認)', () => {
  const { polygons } = buildAll();
  const poly = polygons.get('PA-1.5').points; // upper: H=12〜7, lower: H=4
  assert.equal(containsPoint(poly, 0.3, 7), true);
  assert.equal(containsPoint(poly, 0.3, 20), false);
  assert.equal(containsPoint(poly, 1.5, 5), false);
});

test('フィルタとの組み合わせ: series=R は4モデル', () => {
  const { ds } = buildAll();
  const r = applyFilters(ds.models, { series: new Set(['R']) });
  assert.equal(r.length, 4);
});
