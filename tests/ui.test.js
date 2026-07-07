// UI層のヘッドレステスト(jsdom)。index.htmlを読み込み、main.jsを実際に起動して操作を再現する。
// ブラウザ実機なしでUIの回帰を検知するための正式スイート(旧scratchpadスモークの昇格版)。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import * as d3 from 'd3';
import * as XLSX from 'xlsx';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { calibrate, imageToData, fitMapping, IMAGE_SPACE } from '../src/ui/digitizer.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const html = readFileSync(path.join(ROOT, 'index.html'), 'utf8').replace(/<script[^>]*src[^>]*><\/script>/g, '');
const dom = new JSDOM(html, { url: 'http://localhost/', pretendToBeVisual: true });

global.window = dom.window;
global.document = dom.window.document;
global.d3 = d3;
global.XLSX = XLSX;
global.XMLSerializer = dom.window.XMLSerializer;
global.FileReader = dom.window.FileReader;

// jsdomはレイアウトを持たないため寸法をスタブ(チャートは1000×600で描画される)
Object.defineProperty(dom.window.HTMLElement.prototype, 'clientWidth', { get: () => 1000 });
Object.defineProperty(dom.window.HTMLElement.prototype, 'clientHeight', { get: () => 600 });

await import('../src/ui/main.js');

const $ = (id) => document.getElementById(id);
const fire = (elm, type) => elm.dispatchEvent(new dom.window.Event(type));
const clickAt = (cx, cy) =>
  $('chart').dispatchEvent(new dom.window.MouseEvent('click', { clientX: cx, clientY: cy }));
const polygons = () => document.querySelectorAll('#chart path.polygon');
const settle = () => new Promise((r) => setTimeout(r, 30));

// チャート座標系(chart.jsのMARGINと軸範囲から画面座標を計算)
const MARGIN = { top: 20, right: 24, bottom: 52, left: 60 };
const IW = 1000 - MARGIN.left - MARGIN.right;
const IH = 600 - MARGIN.top - MARGIN.bottom;
const DOMAIN = { xmin: 0.01, xmax: 10, ymin: 1, ymax: 100 }; // テストで軸を固定する値
const sx = (q) => MARGIN.left + (IW * Math.log(q / DOMAIN.xmin)) / Math.log(DOMAIN.xmax / DOMAIN.xmin);
const sy = (h) => MARGIN.top + IH * (1 - Math.log(h / DOMAIN.ymin) / Math.log(DOMAIN.ymax / DOMAIN.ymin));

test('サンプル読み込み→10ポリゴンとラベルが描画される', () => {
  fire($('btn-sample'), 'click');
  assert.equal(polygons().length, 10);
  const labels = [...document.querySelectorAll('#chart text')].map((t) => t.textContent);
  assert.ok(labels.includes('PA-5.5') && labels.includes('PC-3.7'));
  assert.ok($('errors').hidden);
  assert.match($('status-bar').textContent, /表示 10\/10/);
});

test('フィルタ: チェックを外すと即時再描画される', () => {
  const cb = document.querySelector('#filters fieldset input[type=checkbox]'); // series=M
  cb.checked = false;
  fire(cb, 'change');
  assert.equal(polygons().length, 7);
  cb.checked = true;
  fire(cb, 'change');
  assert.equal(polygons().length, 10);
});

test('外包モード: シリーズ3グループ、任意列polesで2グループ', () => {
  const radio = document.querySelector('input[name=mode][value=envelope]');
  radio.checked = true;
  fire(radio, 'change');
  assert.equal(polygons().length, 3);
  const sel = $('envelope-by');
  assert.ok([...sel.options].some((o) => o.value === 'poles'));
  sel.value = 'poles';
  fire(sel, 'change');
  assert.equal(polygons().length, 2);
  const ind = document.querySelector('input[name=mode][value=individual]');
  ind.checked = true;
  fire(ind, 'change');
});

test('クリック選択→非選択の減光→背景クリックで解除', () => {
  document.querySelector('#chart path.polygon').dispatchEvent(new dom.window.MouseEvent('click'));
  assert.match($('status-bar').textContent, /選択 1/);
  const dimmed = [...polygons()].filter((p) => p.getAttribute('opacity') === '0.15');
  assert.equal(dimmed.length, 9);
  clickAt(5, 5); // 背景クリック
  assert.match($('status-bar').textContent, /選択 0/);
});

test('軸範囲の手動上書き', () => {
  for (const [id, v] of [['axis-xmin', DOMAIN.xmin], ['axis-xmax', DOMAIN.xmax], ['axis-ymin', DOMAIN.ymin], ['axis-ymax', DOMAIN.ymax]]) {
    $(id).value = String(v);
    fire($(id), 'change');
  }
  assert.equal(polygons().length, 10);
});

test('デジタイザ: 画像読み込み→仮表示', async () => {
  const file = new dom.window.File(['fake-image-bytes'], 'scan.png', { type: 'image/png' });
  const input = $('digi-image');
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  fire(input, 'change');
  await settle(); // FileReaderの完了待ち
  assert.ok(!$('digi-body').hidden, 'デジタイザUIが表示される');
  assert.ok(document.querySelector('#chart image'), '背景画像が描画される');
});

test('デジタイザ: 2点較正で画像配置が正しく更新される', () => {
  // 点1: 実図上の (Q=0.1, H=10) を、チャート上の (0.2, 20) の位置でクリック(=画像がずれている状況)
  $('digi-q1').value = '0.1';
  $('digi-h1').value = '10';
  fire($('digi-set1'), 'click');
  clickAt(sx(0.2), sy(20));
  assert.match($('digi-calib-status').textContent, /もう一方/);
  // 点2: 実図上の (Q=2, H=2) を、チャート上の (1, 2) の位置でクリック
  $('digi-q2').value = '2';
  $('digi-h2').value = '2';
  fire($('digi-set2'), 'click');
  clickAt(sx(1), sy(2));
  assert.match($('digi-calib-status').textContent, /較正済み/);

  // 期待される写像を独立に計算して、<image>のx座標が一致することを確認
  const fit = fitMapping(DOMAIN);
  const toPx = (q) => (Math.log(q) - fit.bx) / fit.ax;
  const toPy = (h) => (Math.log(h) - fit.by) / fit.ay;
  const expected = calibrate(
    { px: toPx(0.2), py: toPy(20), q: 0.1, h: 10 },
    { px: toPx(1), py: toPy(2), q: 2, h: 2 }
  );
  const c0 = imageToData(expected, 0, 0);
  const c1 = imageToData(expected, IMAGE_SPACE.width, IMAGE_SPACE.height);
  const expectedX = Math.min(sx(c0.flow), sx(c1.flow)) - MARGIN.left; // <image>はプロット群(g)内座標
  const actualX = Number(document.querySelector('#chart image').getAttribute('x'));
  assert.ok(Math.abs(actualX - expectedX) < 0.5, `image x: ${actualX} ≠ 期待 ${expectedX}`);
});

test('デジタイザ: 点の採取→undo→Excel出力の行が正しい', () => {
  $('digi-model').value = '99Z-1.5';
  $('digi-curve').value = 'upper';
  const cap = $('digi-capture');
  cap.checked = true;
  fire(cap, 'change');

  clickAt(sx(0.5), sy(5));
  assert.match($('digi-count').textContent, /採取点 1/);
  clickAt(sx(0.9), sy(8));
  clickAt(sx(1.2), sy(3));
  assert.match($('digi-count').textContent, /採取点 3/);
  fire($('digi-undo'), 'click');
  assert.match($('digi-count').textContent, /採取点 2/);

  // XLSX.writeFileを差し替えて出力内容を検査
  let captured = null;
  global.XLSX = { ...XLSX, writeFile: (wb) => (captured = wb) };
  fire($('digi-export'), 'click');
  global.XLSX = XLSX;
  assert.ok(captured, 'ワークブックが生成される');
  const rows = XLSX.utils.sheet_to_json(captured.Sheets['curves']);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].model, '99Z-1.5');
  assert.ok(Math.abs(rows[0].flow - 0.5) < 1e-6 && Math.abs(rows[0].head - 5) < 1e-6);
  const modelRows = XLSX.utils.sheet_to_json(captured.Sheets['models']);
  assert.equal(modelRows[0].model, '99Z-1.5');

  cap.checked = false;
  fire(cap, 'change');
});

test('採取モード中はポリゴン選択が無効化される', () => {
  const cap = $('digi-capture');
  cap.checked = true;
  fire(cap, 'change');
  assert.equal(document.querySelector('#chart path.polygon').getAttribute('pointer-events'), 'none');
  cap.checked = false;
  fire(cap, 'change');
  assert.equal(document.querySelector('#chart path.polygon').getAttribute('pointer-events'), null);
});
