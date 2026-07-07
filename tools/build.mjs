// 配布用ビルド: 全アセットを埋め込んだ自己完結の単一HTMLを dist/ に生成する。
// 生成物はダブルクリック(file://)で動作し、外部への通信は一切行わない(NFR-2/NFR-3)。
// 使い方: npm run build
import { build } from 'esbuild';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const read = (p) => readFileSync(path.join(ROOT, p), 'utf8');

const pkg = JSON.parse(read('package.json'));
const version = pkg.version;
const buildDate = new Date().toISOString().slice(0, 10);

// インラインscriptを閉じてしまう文字列が混入していないか防御(現状のvendorには存在しない)
const assertInlinable = (name, code) => {
  if (code.includes('</script')) throw new Error(`${name} に </script が含まれるためインライン化できません`);
  return code;
};

const result = await build({
  entryPoints: [path.join(ROOT, 'src/ui/main.js')],
  bundle: true,
  format: 'iife',
  minify: true,
  write: false,
  legalComments: 'none',
});
const appJs = assertInlinable('app bundle', result.outputFiles[0].text);

let html = read('index.html');
html = html
  .replace(/<link rel="stylesheet"[^>]*>/, () => `<style>\n${read('styles.css')}\n</style>`)
  .replace(/<script src="\.\/src\/vendor\/d3[^"]*"><\/script>/, () => `<script>\n${assertInlinable('d3', read('src/vendor/d3.v7.min.js'))}\n</script>`)
  .replace(/<script src="\.\/src\/vendor\/xlsx[^"]*"><\/script>/, () => `<script>\n${assertInlinable('xlsx', read('src/vendor/xlsx.full.min.js'))}\n</script>`)
  .replace(/<script type="module"[^>]*><\/script>/, () => `<script>\n${appJs}\n</script>`)
  .replace('>dev版<', `>v${version} (${buildDate})<`);

// 埋め込み漏れがないか検査(外部読み込みタグが残っていたら失敗させる)
if (/<script[^>]+src=|<link[^>]+href=/.test(html)) {
  throw new Error('外部読み込みタグが残っています。配布物として不完全です');
}

mkdirSync(path.join(ROOT, 'dist'), { recursive: true });
const outName = `curve-chart-v${version}.html`;
writeFileSync(path.join(ROOT, 'dist', outName), html);
console.log(`dist/${outName} (${(html.length / 1024 / 1024).toFixed(2)} MB)`);
