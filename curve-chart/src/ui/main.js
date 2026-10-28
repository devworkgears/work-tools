// 状態管理層 + UI組み立て。単方向データフロー: UIイベント → 状態更新 → update()
/* global d3, XLSX */
import { parseDataset, axisFromConfig } from '../domain/parser.js';
import { deriveLower, buildPolygon } from '../domain/geometry.js';
import { buildEnvelope } from '../domain/envelope.js';
import { getFilterableColumns, distinctValues, applyFilters, getModelValue, groupBy } from '../domain/query.js';
import { renderChart } from './chart.js';
import { downloadSVG, downloadPNG } from './exporter.js';
import { sampleSheets, templateReadmeRows } from '../sample-data.js';
import { calibrate, dataToImage, fitMapping, buildExportRows } from './digitizer.js';

const COLUMN_LABELS = { series: 'シリーズ', output_kw: '出力 [kW]', diameter_mm: '口径 [mm]' };
const columnLabel = (col) => COLUMN_LABELS[col] ?? col;

const state = {
  fileName: null,
  dataset: null, // parseDataset の結果
  polygons: new Map(), // model名 -> { points, rule }
  loadErrors: [], // 読み込み時エラー(parse + lower導出)
  filters: {}, // 列名 -> Set<string>(チェック中の値)
  mode: 'individual', // 'individual' | 'envelope'
  envelopeBy: 'series',
  envelopeMethod: 'union', // 'union'(合併境界) | 'hull'(凸包)。未決事項#3の比較用に両対応
  selection: new Set(),
  axis: { xmin: null, xmax: null, ymin: null, ymax: null },
  // 実図デジタイザ(v0.3 実データ検証支援)
  digitizer: {
    href: null, // 画像のdata URL
    mapping: null, // 画像ピクセル↔実座標の写像(digitizer.js)
    scales: { q: 'log', h: 'log' }, // 実図の軸種別
    opacity: 0.5,
    p1: null, // 較正基準点 {px, py, q, h}
    p2: null,
    pending: null, // 'p1' | 'p2' | null … 次のクリックで指定する基準点
    capture: false,
    points: [], // 採取済み [{model, curve, flow, head}]
  },
};

const colorScale = d3.scaleOrdinal(d3.schemeTableau10);
const el = (id) => document.getElementById(id);

// ---------- データ読み込み ----------

function workbookToSheets(wb) {
  const rows = (name) => {
    if (!wb.Sheets[name]) return null;
    return XLSX.utils
      .sheet_to_json(wb.Sheets[name], { defval: null })
      .map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [String(k).trim(), v])));
  };
  return { models: rows('models'), curves: rows('curves'), config: rows('config') ?? [] };
}

function loadSheets(sheets, sourceName) {
  const ds = parseDataset(sheets);
  const errors = [...ds.errors];
  const polygons = new Map();
  for (const m of ds.models) {
    const entry = ds.curvesByModel[m.model];
    if (!entry || !entry.upper || entry.upper.length < 2) continue;
    const lower = deriveLower(m, ds.curvesByModel, ds.config);
    if (lower.error) {
      errors.push({ sheet: 'models', row: null, model: m.model, message: `lower導出失敗: ${lower.error}` });
      continue;
    }
    polygons.set(m.model, { points: buildPolygon(entry.upper, lower.points), rule: lower.rule });
  }
  state.dataset = ds;
  state.polygons = polygons;
  state.loadErrors = errors;
  state.selection = new Set();
  state.fileName = sourceName;
  // configの軸固定範囲を初期値として反映(UIからの上書きは引き続き可能)
  state.axis = axisFromConfig(ds.config);
  for (const [k, v] of Object.entries(state.axis)) el(`axis-${k}`).value = v ?? '';
  el('file-name').textContent = `${sourceName}(${polygons.size}モデル)`;
  el('empty-hint').hidden = polygons.size > 0;
  buildFilterPanel();
  buildEnvelopeSelect();
  renderErrors();
  update();
}

async function handleFile(file) {
  try {
    const wb = XLSX.read(await file.arrayBuffer());
    loadSheets(workbookToSheets(wb), file.name);
  } catch (e) {
    state.loadErrors = [{ sheet: '-', row: null, model: null, message: `ファイルを読み込めません: ${e.message}` }];
    renderErrors();
  }
}

function downloadTemplate() {
  const { models, curves, config } = sampleSheets();
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(models), 'models');
  XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(curves), 'curves');
  XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(config), 'config');
  XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(templateReadmeRows()), '説明');
  XLSX.writeFile(wb, 'curve-chart-template.xlsx');
}

// ---------- サイドバーUI ----------

function drawableModels() {
  return state.dataset ? state.dataset.models.filter((m) => state.polygons.has(m.model)) : [];
}

function buildFilterPanel() {
  const wrap = el('filters');
  wrap.textContent = '';
  const models = drawableModels();
  state.filters = {};
  for (const col of getFilterableColumns(models)) {
    const values = distinctValues(models, col);
    if (values.length === 0) continue;
    state.filters[col] = new Set(values.map(String));

    const fs = document.createElement('fieldset');
    const legend = document.createElement('legend');
    legend.textContent = columnLabel(col);
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'mini';
    toggle.textContent = '全/無';
    toggle.addEventListener('click', () => {
      const set = state.filters[col];
      const allOn = set.size === values.length;
      set.clear();
      if (!allOn) values.forEach((v) => set.add(String(v)));
      fs.querySelectorAll('input').forEach((cb) => (cb.checked = !allOn));
      state.selection.clear();
      update();
    });
    legend.appendChild(toggle);
    fs.appendChild(legend);

    for (const v of values) {
      const label = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = true;
      cb.addEventListener('change', () => {
        const set = state.filters[col];
        cb.checked ? set.add(String(v)) : set.delete(String(v));
        state.selection.clear();
        update();
      });
      label.appendChild(cb);
      if (col === 'series') {
        // 凡例を兼ねる色スウォッチ(FR-4.5)
        const sw = document.createElement('span');
        sw.className = 'swatch';
        sw.style.background = colorScale(String(v));
        label.appendChild(sw);
      }
      label.appendChild(document.createTextNode(` ${v}`));
      fs.appendChild(label);
    }
    wrap.appendChild(fs);
  }
  if (!wrap.hasChildNodes()) wrap.textContent = 'データ未読み込み';
}

function buildEnvelopeSelect() {
  const sel = el('envelope-by');
  sel.textContent = '';
  for (const col of getFilterableColumns(drawableModels())) {
    const opt = document.createElement('option');
    opt.value = col;
    opt.textContent = columnLabel(col);
    sel.appendChild(opt);
  }
  sel.value = state.envelopeBy = 'series';
}

function renderErrors() {
  const box = el('errors');
  box.textContent = '';
  box.hidden = state.loadErrors.length === 0;
  if (box.hidden) return;
  const title = document.createElement('div');
  title.className = 'errors-title';
  title.textContent = `読み込みエラー ${state.loadErrors.length}件`;
  box.appendChild(title);
  const ul = document.createElement('ul');
  for (const e of state.loadErrors) {
    const li = document.createElement('li');
    const loc = [e.sheet, e.row ? `${e.row}行目` : null, e.model ? `[${e.model}]` : null].filter(Boolean).join(' ');
    li.textContent = `${loc}: ${e.message}`;
    ul.appendChild(li);
  }
  box.appendChild(ul);
}

// ---------- 描画アイテムの構築 ----------

function buildItems() {
  const models = applyFilters(drawableModels(), state.filters);
  const dim = (id) => state.selection.size > 0 && !state.selection.has(id);

  if (state.mode === 'individual') {
    return {
      models,
      items: models.map((m) => ({
        id: m.model,
        label: m.model,
        color: colorScale(m.series),
        rings: [state.polygons.get(m.model).points],
        meta: { type: 'model', model: m, rule: state.polygons.get(m.model).rule },
        selected: state.selection.has(m.model),
        dimmed: dim(m.model),
      })),
    };
  }

  // グループ外包モード: 凸包 or 合併境界(FR-3.2、方式は未決事項#3 → 両対応でトグル比較)
  const items = [];
  for (const [key, ms] of groupBy(models, state.envelopeBy)) {
    const rings = buildEnvelope(
      ms.map((m) => state.polygons.get(m.model).points),
      state.envelopeMethod
    );
    const id = `group:${key}`;
    items.push({
      id,
      label: key,
      color: colorScale(state.envelopeBy === 'series' ? key : `${state.envelopeBy}:${key}`),
      rings,
      meta: { type: 'group', key, col: state.envelopeBy, models: ms },
      selected: state.selection.has(id),
      dimmed: dim(id),
    });
  }
  return { models, items };
}

// ---------- ツールチップ ----------

/** @param {unknown} value @returns {string} */
function escapeHTML(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
}

function showTooltip(item, ev) {
  const tip = el('tooltip');
  if (!item) {
    tip.hidden = true;
    return;
  }
  const rows = [];
  if (item.meta.type === 'model') {
    const m = item.meta.model;
    rows.push(`<b>${escapeHTML(m.model)}</b>`);
    rows.push(`シリーズ: ${escapeHTML(m.series)}`);
    rows.push(`出力: ${m.output_kw} kW`);
    rows.push(`口径: ${m.diameter_mm} mm`);
    for (const [k, v] of Object.entries(m.extras)) rows.push(`${escapeHTML(k)}: ${escapeHTML(v)}`);
    rows.push(`<span class="muted">下限ルール: ${escapeHTML(item.meta.rule)}</span>`);
  } else {
    rows.push(`<b>${escapeHTML(columnLabel(item.meta.col))} = ${escapeHTML(item.meta.key)}</b>`);
    rows.push(`${item.meta.models.length}モデル: ${item.meta.models.map((m) => escapeHTML(m.model)).join(', ')}`);
  }
  tip.innerHTML = rows.join('<br>');
  tip.hidden = false;
  const wrap = el('chart-wrap').getBoundingClientRect();
  const tx = Math.min(ev.clientX - wrap.left + 14, wrap.width - tip.offsetWidth - 8);
  const ty = Math.min(ev.clientY - wrap.top + 14, wrap.height - tip.offsetHeight - 8);
  tip.style.left = `${Math.max(0, tx)}px`;
  tip.style.top = `${Math.max(0, ty)}px`;
}

// ---------- 再描画 ----------

const fmt = d3.format('.3~g');

// 現在の軸範囲(デジタイザの仮写像・クリック座標変換に使う)
function currentDomain() {
  const models = state.dataset ? applyFilters(drawableModels(), state.filters) : [];
  const pts = models.flatMap((m) => state.polygons.get(m.model).points);
  let [xmin, xmax] = [0.05, 5];
  let [ymin, ymax] = [1, 100];
  if (pts.length) {
    [xmin, xmax] = d3.extent(pts, (p) => p.flow);
    [ymin, ymax] = d3.extent(pts, (p) => p.head);
    xmin *= 0.85;
    xmax *= 1.18;
    ymin *= 0.85;
    ymax *= 1.18;
  }
  return {
    xmin: state.axis.xmin ?? xmin,
    xmax: state.axis.xmax ?? xmax,
    ymin: state.axis.ymin ?? ymin,
    ymax: state.axis.ymax ?? ymax,
  };
}

function update() {
  const wrap = el('chart-wrap');
  const { models, items } = state.dataset ? buildItems() : { models: [], items: [] };
  const d = state.digitizer;
  renderChart(el('chart'), items, {
    width: wrap.clientWidth,
    height: wrap.clientHeight,
    axis: state.axis,
    underlay: d.href ? { href: d.href, mapping: d.mapping, opacity: d.opacity } : null,
    captureMode: d.pending !== null || d.capture,
    onHover: showTooltip,
    onClick: (item) => {
      if (!item) state.selection.clear();
      else state.selection.has(item.id) ? state.selection.delete(item.id) : state.selection.add(item.id);
      update();
    },
    onPlotClick: handlePlotClick,
    onMove: (q, h) => {
      el('status-coords').textContent = q === null ? '' : `Q=${fmt(q)} m³/min  H=${fmt(h)} m`;
    },
  });
  const total = drawableModels().length;
  const modeLabel = state.mode === 'individual' ? '個別モデル' : `グループ外包(${columnLabel(state.envelopeBy)})`;
  const selNames = [...state.selection].map((id) => id.replace(/^group:/, ''));
  const selLabel = state.selection.size === 0 ? '選択 0' : `選択 ${state.selection.size}(${selNames.slice(0, 4).join(', ')}${selNames.length > 4 ? ', …' : ''})`;
  el('status-left').textContent = state.dataset
    ? `${modeLabel} | 表示 ${models.length}/${total} モデル | ${selLabel}`
    : '';
}

// ---------- 実図デジタイザ ----------

function digiStatus(text) {
  el('digi-calib-status').textContent = text;
}

function updateDigiCount() {
  el('digi-count').textContent = `採取点 ${state.digitizer.points.length}`;
}

// リニア軸を含む較正時の表示
function linearNote() {
  const s = state.digitizer.scales;
  return s.q === 'linear' || s.h === 'linear'
    ? '(リニア軸)'
    : '';
}

// 較正基準点のQ/H入力値を検証して返す。対数軸は正の値のみ、リニア軸は0や負も許容
function readCalibValue(inputId, scale) {
  const raw = el(inputId).value.trim();
  if (raw === '') return null;
  const v = Number(raw);
  if (!Number.isFinite(v)) return null;
  if (scale !== 'linear' && v <= 0) return null;
  return v;
}

function recalibrate() {
  const d = state.digitizer;
  const m = calibrate(d.p1, d.p2, d.scales);
  if (m) {
    d.mapping = m;
    digiStatus('較正済み' + linearNote());
  } else {
    digiStatus('較正できません: 2点のQ同士・H同士が同一、クリック位置が同一、または対数軸に0以下の値があります');
  }
}

// 採取・較正モード中のチャートクリック(q, h は現在の軸スケールでの実座標)
function handlePlotClick(q, h) {
  const d = state.digitizer;
  if (d.pending) {
    const n = d.pending === 'p1' ? 1 : 2;
    const qv = readCalibValue(`digi-q${n}`, d.scales.q);
    const hv = readCalibValue(`digi-h${n}`, d.scales.h);
    if (qv === null || hv === null) {
      digiStatus(`点${n}のQ・Hを先に入力してください(対数軸は正の値のみ)`);
      d.pending = null;
      update();
      return;
    }
    // クリック位置を現在の写像で画像ピクセルに変換して基準点として記録
    const { px, py } = dataToImage(d.mapping, q, h);
    d[d.pending] = { px, py, q: qv, h: hv };
    d.pending = null;
    if (d.p1 && d.p2) {
      recalibrate();
    } else {
      digiStatus(`点${n}を記録しました。もう一方の基準点を指定してください`);
    }
    update();
    return;
  }
  if (d.capture) {
    const model = el('digi-model').value.trim();
    if (!model) {
      digiStatus('モデル名を入力してから採取してください');
      return;
    }
    d.points.push({
      model,
      curve: el('digi-curve').value,
      flow: Number(q.toPrecision(4)),
      head: Number(h.toPrecision(4)),
    });
    updateDigiCount();
  }
}

function initDigitizer() {
  el('digi-image').addEventListener('change', (ev) => {
    const file = ev.target.files[0];
    ev.target.value = '';
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const d = state.digitizer;
      d.href = reader.result;
      d.mapping = fitMapping(currentDomain());
      d.p1 = null;
      d.p2 = null;
      el('digi-body').hidden = false;
      digiStatus('未較正');
      update();
    };
    reader.readAsDataURL(file);
  });
  el('digi-opacity').addEventListener('input', (ev) => {
    state.digitizer.opacity = Number(ev.target.value) / 100;
    update();
  });
  // 軸種別の変更: 既に2点あればその場で再較正
  for (const id of ['digi-qscale', 'digi-hscale']) {
    el(id).addEventListener('change', () => {
      const d = state.digitizer;
      d.scales = { q: el('digi-qscale').value, h: el('digi-hscale').value };
      if (d.p1 && d.p2) {
        recalibrate();
        update();
      }
    });
  }
  el('digi-set1').addEventListener('click', () => {
    state.digitizer.pending = 'p1';
    digiStatus('チャート上で「実図の点1」の位置をクリックしてください');
    update();
  });
  el('digi-set2').addEventListener('click', () => {
    state.digitizer.pending = 'p2';
    digiStatus('チャート上で「実図の点2」の位置をクリックしてください');
    update();
  });
  el('digi-capture').addEventListener('change', (ev) => {
    state.digitizer.capture = ev.target.checked;
    update();
  });
  el('digi-undo').addEventListener('click', () => {
    state.digitizer.points.pop();
    updateDigiCount();
  });
  el('digi-clear').addEventListener('click', () => {
    state.digitizer.points = [];
    updateDigiCount();
  });
  el('digi-export').addEventListener('click', () => {
    const { models, curves } = buildExportRows(state.digitizer.points);
    if (curves.length === 0) {
      digiStatus('採取点がありません');
      return;
    }
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(models), 'models');
    XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(curves), 'curves');
    XLSX.writeFile(wb, 'digitized-curves.xlsx');
  });
}

// ---------- イベント配線 ----------

function init() {
  el('file-input').addEventListener('change', (ev) => {
    if (ev.target.files[0]) handleFile(ev.target.files[0]);
    ev.target.value = '';
  });
  el('btn-template').addEventListener('click', downloadTemplate);
  el('btn-sample').addEventListener('click', () => loadSheets(sampleSheets(), 'サンプルデータ'));

  document.querySelectorAll('input[name="mode"]').forEach((r) =>
    r.addEventListener('change', () => {
      state.mode = r.value;
      state.selection.clear();
      el('envelope-config').hidden = state.mode !== 'envelope';
      update();
    })
  );
  el('envelope-by').addEventListener('change', (ev) => {
    state.envelopeBy = ev.target.value;
    state.selection.clear();
    update();
  });
  document.querySelectorAll('input[name="envelope-method"]').forEach((r) =>
    r.addEventListener('change', () => {
      state.envelopeMethod = r.value;
      update();
    })
  );

  for (const key of ['xmin', 'xmax', 'ymin', 'ymax']) {
    el(`axis-${key}`).addEventListener('change', (ev) => {
      const n = Number(ev.target.value);
      state.axis[key] = ev.target.value !== '' && Number.isFinite(n) && n > 0 ? n : null;
      update();
    });
  }
  el('btn-axis-reset').addEventListener('click', () => {
    state.axis = { xmin: null, xmax: null, ymin: null, ymax: null };
    ['xmin', 'xmax', 'ymin', 'ymax'].forEach((k) => (el(`axis-${k}`).value = ''));
    update();
  });

  el('btn-svg').addEventListener('click', () => downloadSVG(el('chart')));
  el('btn-png').addEventListener('click', () => downloadPNG(el('chart')));

  // ドラッグ&ドロップ対応(FR-1.1)
  const wrap = el('chart-wrap');
  wrap.addEventListener('dragover', (ev) => ev.preventDefault());
  wrap.addEventListener('drop', (ev) => {
    ev.preventDefault();
    const f = ev.dataTransfer.files[0];
    if (f) handleFile(f);
  });

  initDigitizer();

  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(update, 100);
  });

  update();
}

init();
