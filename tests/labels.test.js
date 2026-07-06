import { test } from 'node:test';
import assert from 'node:assert/strict';
import { separateLabels } from '../src/ui/chart.js';

const overlaps = (a, b, pad = 0) =>
  Math.abs(a.x - b.x) < (a.w + b.w) / 2 + pad && Math.abs(a.y - b.y) < (a.h + b.h) / 2 + pad;

test('separateLabels: 重なった2ラベルが分離される', () => {
  const boxes = [
    { x: 100, y: 100, w: 60, h: 14 },
    { x: 105, y: 103, w: 60, h: 14 },
  ];
  separateLabels(boxes, 800, 600);
  assert.equal(overlaps(boxes[0], boxes[1]), false);
});

test('separateLabels: 重なっていないラベルは動かない', () => {
  const boxes = [
    { x: 100, y: 100, w: 60, h: 14 },
    { x: 300, y: 300, w: 60, h: 14 },
  ];
  separateLabels(boxes, 800, 600);
  assert.deepEqual(boxes[0], { x: 100, y: 100, w: 60, h: 14 });
  assert.deepEqual(boxes[1], { x: 300, y: 300, w: 60, h: 14 });
});

test('separateLabels: 密集した10ラベルも全ペア分離される', () => {
  const boxes = Array.from({ length: 10 }, (_, i) => ({ x: 200 + i * 3, y: 200 + i * 2, w: 55, h: 14 }));
  separateLabels(boxes, 800, 600);
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      assert.equal(overlaps(boxes[i], boxes[j]), false, `box${i} と box${j} が重なったまま`);
    }
  }
});

test('separateLabels: 描画領域内にクランプされる', () => {
  const boxes = [{ x: -10, y: 700, w: 60, h: 14 }];
  separateLabels(boxes, 800, 600);
  assert.ok(boxes[0].x >= 30 && boxes[0].y <= 600 - 7);
});
