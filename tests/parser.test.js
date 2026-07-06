import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseDataset } from '../src/domain/parser.js';
import { sampleSheets } from '../src/sample-data.js';

test('サンプルデータはエラーなしで読み込める', () => {
  const ds = parseDataset(sampleSheets());
  assert.equal(ds.errors.length, 0, JSON.stringify(ds.errors, null, 2));
  assert.equal(ds.models.length, 10);
  assert.equal(ds.config.default_lower_rule, 'clip');
  assert.ok(ds.curvesByModel['PA-1.5'].upper.length === 5);
  assert.ok(ds.curvesByModel['PC-1.5'].lower.length === 4);
});

test('modelsシート欠落はエラー', () => {
  const ds = parseDataset({ curves: [] });
  assert.ok(ds.errors.some((e) => e.sheet === 'models'));
});

test('必須列の空欄は行番号つきエラー', () => {
  const ds = parseDataset({
    models: [{ model: 'A', series: 'X', output_kw: 1.5, diameter_mm: null }],
    curves: [],
  });
  const e = ds.errors.find((e) => e.sheet === 'models');
  assert.ok(e);
  assert.equal(e.row, 2);
  assert.match(e.message, /diameter_mm/);
});

test('数値でない output_kw はエラー', () => {
  const ds = parseDataset({
    models: [{ model: 'A', series: 'X', output_kw: 'abc', diameter_mm: 50 }],
    curves: [],
  });
  assert.ok(ds.errors.some((e) => /output_kw/.test(e.message)));
});

test('model重複はエラー', () => {
  const rows = { model: 'A', series: 'X', output_kw: 1, diameter_mm: 50 };
  const ds = parseDataset({ models: [rows, { ...rows }], curves: [] });
  assert.ok(ds.errors.some((e) => /重複/.test(e.message)));
  assert.equal(ds.models.length, 1);
});

test('未知のlower_ruleはエラー', () => {
  const ds = parseDataset({
    models: [{ model: 'A', series: 'X', output_kw: 1, diameter_mm: 50, lower_rule: 'magic' }],
    curves: [],
  });
  assert.ok(ds.errors.some((e) => /lower_rule/.test(e.message)));
});

test('curvesの未知モデル・非数値・非正値はエラー', () => {
  const models = [{ model: 'A', series: 'X', output_kw: 1, diameter_mm: 50 }];
  const ds = parseDataset({
    models,
    curves: [
      { model: 'B', curve: 'upper', flow: 0.1, head: 10 },
      { model: 'A', curve: 'upper', flow: 'x', head: 10 },
      { model: 'A', curve: 'upper', flow: -0.1, head: 10 },
    ],
  });
  assert.ok(ds.errors.some((e) => /存在しないモデル/.test(e.message) && e.row === 2));
  assert.ok(ds.errors.some((e) => /数値ではありません/.test(e.message) && e.row === 3));
  assert.ok(ds.errors.some((e) => /正の値/.test(e.message) && e.row === 4));
});

test('upperカーブ欠落・点数不足はエラー', () => {
  const ds = parseDataset({
    models: [
      { model: 'A', series: 'X', output_kw: 1, diameter_mm: 50 },
      { model: 'B', series: 'X', output_kw: 2, diameter_mm: 50 },
    ],
    curves: [{ model: 'B', curve: 'upper', flow: 0.1, head: 10 }],
  });
  assert.ok(ds.errors.some((e) => e.model === 'A' && /upperカーブがありません/.test(e.message)));
  assert.ok(ds.errors.some((e) => e.model === 'B' && /2点以上/.test(e.message)));
});

test('flow順が乱れていても自動ソートされる(検証項目6)', () => {
  const ds = parseDataset({
    models: [{ model: 'A', series: 'X', output_kw: 1, diameter_mm: 50, lower_rule: 'clip', lower_param: 1 }],
    curves: [
      { model: 'A', curve: 'upper', flow: 0.5, head: 7 },
      { model: 'A', curve: 'upper', flow: 0.1, head: 12 },
      { model: 'A', curve: 'upper', flow: 0.3, head: 10 },
    ],
  });
  assert.deepEqual(
    ds.curvesByModel['A'].upper.map((p) => p.flow),
    [0.1, 0.3, 0.5]
  );
});

test('任意列はextrasに入る(FR-4.6)', () => {
  const ds = parseDataset({
    models: [{ model: 'A', series: 'X', output_kw: 1, diameter_mm: 50, poles: 2, freq_hz: 60 }],
    curves: [],
  });
  assert.deepEqual(ds.models[0].extras, { poles: 2, freq_hz: 60 });
});

test('axisFromConfig: 軸固定範囲の読み取りと不正値の無視', async () => {
  const { axisFromConfig } = await import('../src/domain/parser.js');
  assert.deepEqual(axisFromConfig({ axis_qmin: 0.05, axis_qmax: 3, axis_hmin: 2, axis_hmax: 80 }), {
    xmin: 0.05, xmax: 3, ymin: 2, ymax: 80,
  });
  assert.deepEqual(axisFromConfig({ axis_qmin: 'abc', axis_hmax: -5 }), {
    xmin: null, xmax: null, ymin: null, ymax: null,
  });
  assert.deepEqual(axisFromConfig({}), { xmin: null, xmax: null, ymin: null, ymax: null });
});
