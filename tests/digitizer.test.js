import { test } from 'node:test';
import assert from 'node:assert/strict';
import { calibrate, imageToData, dataToImage, fitMapping, buildExportRows, IMAGE_SPACE } from '../src/ui/digitizer.js';

const near = (a, b, tol = 1e-9) => assert.ok(Math.abs(a - b) < tol, `${a} ≠ ${b}`);

test('calibrate: 2基準点から写像を作り、点で往復できる', () => {
  const m = calibrate({ px: 100, py: 700, q: 0.1, h: 5 }, { px: 900, py: 50, q: 2, h: 60 });
  assert.ok(m);
  // 基準点自身が正しく写る
  const d1 = imageToData(m, 100, 700);
  near(d1.flow, 0.1);
  near(d1.head, 5);
  const d2 = imageToData(m, 900, 50);
  near(d2.flow, 2);
  near(d2.head, 60);
  // 往復一致
  const p = dataToImage(m, 0.5, 20);
  const back = imageToData(m, p.px, p.py);
  near(back.flow, 0.5);
  near(back.head, 20);
});

test('calibrate: 対数空間で線形(中点はいずれの軸でも幾何平均)', () => {
  const m = calibrate({ px: 0, py: 750, q: 0.01, h: 1 }, { px: 1000, py: 0, q: 10, h: 100 });
  const mid = imageToData(m, 500, 375);
  near(mid.flow, Math.sqrt(0.01 * 10), 1e-9);
  near(mid.head, Math.sqrt(1 * 100), 1e-9);
});

test('calibrate: 無効入力はnull', () => {
  assert.equal(calibrate(null, { px: 1, py: 1, q: 1, h: 1 }), null);
  assert.equal(calibrate({ px: 1, py: 1, q: 1, h: 1 }, { px: 1, py: 2, q: 2, h: 2 }), null); // px同一
  assert.equal(calibrate({ px: 1, py: 1, q: 1, h: 1 }, { px: 2, py: 2, q: 1, h: 2 }), null); // q同一
  assert.equal(calibrate({ px: 1, py: 1, q: -1, h: 1 }, { px: 2, py: 2, q: 2, h: 2 }), null); // 非正値
});

test('fitMapping: 画像四隅が軸範囲の四隅に写る', () => {
  const domain = { xmin: 0.05, xmax: 5, ymin: 2, ymax: 80 };
  const m = fitMapping(domain);
  const bl = imageToData(m, 0, IMAGE_SPACE.height);
  const tr = imageToData(m, IMAGE_SPACE.width, 0);
  near(bl.flow, domain.xmin);
  near(bl.head, domain.ymin);
  near(tr.flow, domain.xmax);
  near(tr.head, domain.ymax);
});

test('buildExportRows: model→curve→flow順に整列し、modelsスタブを付ける', () => {
  const { models, curves } = buildExportRows([
    { model: 'B', curve: 'upper', flow: 0.3, head: 8 },
    { model: 'A', curve: 'lower', flow: 0.2, head: 4 },
    { model: 'A', curve: 'upper', flow: 0.5, head: 9 },
    { model: 'A', curve: 'upper', flow: 0.1, head: 12 },
  ]);
  assert.deepEqual(curves.map((c) => [c.model, c.curve, c.flow]), [
    ['A', 'lower', 0.2],
    ['A', 'upper', 0.1],
    ['A', 'upper', 0.5],
    ['B', 'upper', 0.3],
  ]);
  assert.deepEqual(models.map((m) => m.model), ['A', 'B']);
  assert.equal(models[0].series, null);
});
