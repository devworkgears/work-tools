import { test } from 'node:test';
import assert from 'node:assert/strict';
import { getFilterableColumns, distinctValues, applyFilters, groupBy } from '../src/domain/query.js';

const models = [
  { model: 'A1', series: 'A', output_kw: 1.5, diameter_mm: 50, extras: { poles: 2 } },
  { model: 'A2', series: 'A', output_kw: 2.2, diameter_mm: 50, extras: { poles: 4 } },
  { model: 'B1', series: 'B', output_kw: 1.5, diameter_mm: 80, extras: { poles: 2 } },
];

test('getFilterableColumns: 基本列 + 任意列(FR-4.6)', () => {
  assert.deepEqual(getFilterableColumns(models), ['series', 'output_kw', 'diameter_mm', 'poles']);
});

test('distinctValues: 数値は数値順', () => {
  assert.deepEqual(distinctValues(models, 'output_kw'), [1.5, 2.2]);
  assert.deepEqual(distinctValues(models, 'series'), ['A', 'B']);
});

test('applyFilters: 複数列AND・同一列OR(FR-4.2)', () => {
  const r1 = applyFilters(models, { series: new Set(['A']) });
  assert.deepEqual(r1.map((m) => m.model), ['A1', 'A2']);
  const r2 = applyFilters(models, { series: new Set(['A']), output_kw: new Set(['1.5']) });
  assert.deepEqual(r2.map((m) => m.model), ['A1']);
  const r3 = applyFilters(models, { poles: new Set(['2']) });
  assert.deepEqual(r3.map((m) => m.model), ['A1', 'B1']);
  const r4 = applyFilters(models, { series: new Set() });
  assert.equal(r4.length, 0);
});

test('groupBy: 属性でグループ化', () => {
  const g = groupBy(models, 'series');
  assert.deepEqual([...g.keys()], ['A', 'B']);
  assert.equal(g.get('A').length, 2);
});
