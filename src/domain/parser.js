// Excel由来の行データを Dataset に変換・検証する(UI非依存の純粋関数)
// 入力: { models: object[], curves: object[], config: object[] } … sheet_to_json 相当の行配列
// 出力: { models, curvesByModel, config, errors }

export const LOWER_RULES = ['explicit', 'neighbor', 'clip', 'ratio', 'trim'];

const MODEL_REQUIRED = ['model', 'series', 'output_kw', 'diameter_mm'];
const MODEL_NUMERIC = ['output_kw', 'diameter_mm'];
const MODEL_KNOWN = [...MODEL_REQUIRED, 'lower_rule', 'lower_param'];
const CURVE_REQUIRED = ['model', 'curve', 'flow', 'head'];

export function isBlank(v) {
  return v === undefined || v === null || String(v).trim() === '';
}

export function toNumber(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (isBlank(v)) return null;
  const n = Number(String(v).trim());
  return Number.isFinite(n) ? n : null;
}

export function parseDataset(sheets) {
  const errors = [];
  const add = (sheet, row, message, model = null) => errors.push({ sheet, row, message, model });

  const modelRows = sheets?.models;
  const curveRows = sheets?.curves;
  const configRows = sheets?.config ?? [];

  if (!Array.isArray(modelRows)) add('models', null, 'modelsシートが見つかりません');
  if (!Array.isArray(curveRows)) add('curves', null, 'curvesシートが見つかりません');
  if (errors.length) return { models: [], curvesByModel: {}, config: {}, errors };

  // --- config (key / value の縦持ち) ---
  const config = {};
  for (const r of configRows) {
    const key = r.key ?? r['キー'];
    if (isBlank(key)) continue;
    config[String(key).trim()] = r.value ?? r['値'] ?? null;
  }

  // --- models ---
  const models = [];
  const seen = new Set();
  modelRows.forEach((r, i) => {
    const row = i + 2; // ヘッダ行が1行目
    let ok = true;
    for (const col of MODEL_REQUIRED) {
      if (isBlank(r[col])) {
        add('models', row, `必須列 ${col} が空です`);
        ok = false;
      }
    }
    if (!ok) return;
    const name = String(r.model).trim();
    if (seen.has(name)) {
      add('models', row, `model「${name}」が重複しています`, name);
      return;
    }
    seen.add(name);
    const m = { model: name, series: String(r.series).trim(), extras: {} };
    for (const col of MODEL_NUMERIC) {
      const n = toNumber(r[col]);
      if (n === null) {
        add('models', row, `${col} が数値ではありません: ${r[col]}`, name);
        ok = false;
      } else {
        m[col] = n;
      }
    }
    m.lower_rule = isBlank(r.lower_rule) ? null : String(r.lower_rule).trim();
    m.lower_param = isBlank(r.lower_param) ? null : r.lower_param;
    if (m.lower_rule && !LOWER_RULES.includes(m.lower_rule)) {
      add('models', row, `lower_rule「${m.lower_rule}」は未対応です(${LOWER_RULES.join(' / ')})`, name);
      ok = false;
    }
    // 任意の追加列はフィルタ候補として extras に保持(FR-4.6)
    for (const [k, v] of Object.entries(r)) {
      if (!MODEL_KNOWN.includes(k) && !k.startsWith('__') && !isBlank(v)) m.extras[k] = v;
    }
    if (ok) models.push(m);
  });

  // --- curves ---
  const curvesByModel = {};
  const modelNames = new Set(models.map((m) => m.model));
  curveRows.forEach((r, i) => {
    const row = i + 2;
    for (const col of CURVE_REQUIRED) {
      if (isBlank(r[col])) {
        add('curves', row, `必須列 ${col} が空です`);
        return;
      }
    }
    const name = String(r.model).trim();
    const curve = String(r.curve).trim().toLowerCase();
    if (curve !== 'upper' && curve !== 'lower') {
      add('curves', row, `curve は upper / lower のみ指定できます: ${r.curve}`, name);
      return;
    }
    if (!modelNames.has(name)) {
      add('curves', row, `modelsシートに存在しないモデルです: ${name}`, name);
      return;
    }
    const flow = toNumber(r.flow);
    const head = toNumber(r.head);
    if (flow === null || head === null) {
      add('curves', row, `flow / head が数値ではありません: (${r.flow}, ${r.head})`, name);
      return;
    }
    if (flow <= 0 || head <= 0) {
      add('curves', row, `flow / head は正の値が必要です(対数軸のため): (${flow}, ${head})`, name);
      return;
    }
    (curvesByModel[name] ??= { upper: [], lower: [] })[curve].push({ flow, head, row });
  });

  // flow昇順ソート(乱れは自動補正)と点数チェック
  for (const [name, entry] of Object.entries(curvesByModel)) {
    entry.upper.sort((a, b) => a.flow - b.flow);
    entry.lower.sort((a, b) => a.flow - b.flow);
    if (entry.lower.length === 0) {
      entry.lower = null;
    } else if (entry.lower.length < 2) {
      add('curves', entry.lower[0].row, 'lowerカーブは2点以上必要です', name);
      entry.lower = null;
    }
  }
  for (const m of models) {
    const entry = curvesByModel[m.model];
    if (!entry || entry.upper.length === 0) {
      add('curves', null, 'upperカーブがありません', m.model);
    } else if (entry.upper.length < 2) {
      add('curves', entry.upper[0].row, 'upperカーブは2点以上必要です', m.model);
    }
  }

  return { models, curvesByModel, config, errors };
}
