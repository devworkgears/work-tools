// 配布用単一HTMLの検証。dist未ビルド時はスキップ(CIでは npm run build 後に実行される)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const pkg = JSON.parse(readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
const distPath = path.join(ROOT, 'dist', `curve-chart-v${pkg.version}.html`);
const skip = existsSync(distPath) ? false : 'dist未ビルド(npm run build 後に有効)';

test('配布物: 自己完結で file:// 相当でも全機能が起動する', { skip }, async () => {
  const html = readFileSync(distPath, 'utf8');
  assert.ok(!/<script[^>]+src=|<link[^>]+href=/.test(html), '外部読み込みタグが残っている');
  assert.ok(html.includes(`v${pkg.version}`), 'バージョン表記がない');

  const { JSDOM } = await import('jsdom');
  const dom = new JSDOM(html, { url: 'file:///curve-chart.html', runScripts: 'dangerously', pretendToBeVisual: true });
  const { window } = dom;
  Object.defineProperty(window.HTMLElement.prototype, 'clientWidth', { get: () => 1000 });
  Object.defineProperty(window.HTMLElement.prototype, 'clientHeight', { get: () => 600 });

  assert.ok(window.d3 && window.XLSX, 'd3/XLSXが埋め込みから初期化されていない');
  window.document.getElementById('btn-sample').dispatchEvent(new window.Event('click'));
  assert.equal(window.document.querySelectorAll('#chart path.polygon').length, 10);
  assert.match(window.document.getElementById('status-bar').textContent, /表示 10\/10/);
});
