// フィルタ・グループ集約(UI非依存の純粋関数)

const BASE_COLUMNS = ['series', 'output_kw', 'diameter_mm'];

// モデル属性の値を取得(基本列 + extras の任意列)
export function getModelValue(model, col) {
  if (col in model && col !== 'extras') return model[col];
  return model.extras?.[col];
}

// フィルタ対象列: 基本3列 + Excelに追加された任意列(FR-4.6)
export function getFilterableColumns(models) {
  const extra = new Set();
  for (const m of models) {
    for (const k of Object.keys(m.extras ?? {})) extra.add(k);
  }
  return [...BASE_COLUMNS, ...[...extra].sort()];
}

// 列の値一覧(数値は数値順、文字列は辞書順)
export function distinctValues(models, col) {
  const values = new Set();
  for (const m of models) {
    const v = getModelValue(m, col);
    if (v !== undefined && v !== null && String(v).trim() !== '') values.add(v);
  }
  const arr = [...values];
  const allNumeric = arr.every((v) => typeof v === 'number' || !Number.isNaN(Number(v)));
  arr.sort(allNumeric ? (a, b) => Number(a) - Number(b) : (a, b) => String(a).localeCompare(String(b), 'ja'));
  return arr;
}

// filters: { 列名: Set<string>(表示する値) }。列にSetがなければその列は絞り込みなし。
// 複数列は AND、同一列内は OR(FR-4.2)
export function applyFilters(models, filters) {
  const entries = Object.entries(filters ?? {}).filter(([, set]) => set instanceof Set);
  return models.filter((m) =>
    entries.every(([col, set]) => set.has(String(getModelValue(m, col))))
  );
}

// key属性でグループ化 → Map<値文字列, Model[]>
export function groupBy(models, col) {
  const map = new Map();
  for (const m of models) {
    const key = String(getModelValue(m, col));
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(m);
  }
  return map;
}
