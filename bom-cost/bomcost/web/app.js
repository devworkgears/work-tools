// BOM 原価モックの画面。原価は API（Python の計算の層）が出した Decimal の文字列をそのまま使い、
// ここでは桁区切りの表示と操作だけをする。
"use strict";

const state = {
  meta: null,
  versions: [],
  scenarios: [],
  scenarioId: null,
  summary: null,
  mode: "diff",
  byBalloon: false,
  view: "tree",
  selected: null, // { code, path }
  expanded: new Set(), // 開いているノードのパス
  childCache: new Map(), // パス → 子の行
  table: null,
  tableSort: { key: null, desc: false },
  tableFilters: {},
  tableColumns: null, // 表示する列のキー
};

// 表示 ------------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Decimal の文字列に桁区切りを入れる（数としては扱わない）
function fmt(text, { sign = false, missing = "未登録" } = {}) {
  if (text === null || text === undefined) return missing;
  let s = String(text);
  let neg = false;
  if (s.startsWith("-")) { neg = true; s = s.slice(1); }
  const [intPart, frac] = s.split(".");
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const body = frac !== undefined ? `${grouped}.${frac}` : grouped;
  const isZero = /^0*(\.0*)?$/.test(s);
  if (neg && !isZero) return `−${body}`;
  if (sign && !isZero) return `＋${body}`;
  return body;
}

function signClass(text) {
  if (text === null || text === undefined) return "missing";
  if (String(text).startsWith("-")) return "neg";
  return /^0*(\.0*)?$/.test(String(text)) ? "" : "pos";
}

function money(text, opts) {
  return el("span", { class: `num ${text === null || text === undefined ? "missing" : ""}` }, fmt(text, opts));
}

function showMessage(text, kind = "error") {
  const box = document.getElementById("message");
  box.textContent = text;
  box.className = `message ${kind === "info" ? "info" : ""}`;
  box.hidden = !text;
}

// API -------------------------------------------------------------------

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const res = await fetch(path, options);
  const type = res.headers.get("Content-Type") || "";
  if (!res.ok) {
    let message = `${res.status}`;
    if (type.includes("json")) message = (await res.json()).error || message;
    throw new Error(message);
  }
  return type.includes("json") ? res.json() : res.blob();
}

function viewQuery(extra = {}) {
  const params = new URLSearchParams({
    mode: state.mode,
    by_balloon: state.byBalloon ? "1" : "0",
    ...extra,
  });
  return params.toString();
}

async function guarded(fn) {
  try {
    showMessage("");
    await fn();
  } catch (err) {
    showMessage(err.message || String(err));
  }
}

// 読み込み ----------------------------------------------------------------

async function init() {
  state.meta = await api("GET", "/api/meta");
  const headSelect = document.getElementById("head-select");
  headSelect.replaceChildren(...state.meta.heads.map((h) => el("option", { value: h.id }, h.label)));
  state.tableColumns = state.meta.table_columns.map((c) => c.key);
  await loadLists();
  if (!state.versions.length) {
    showMessage("版がまだありません。「取り込み・版」の画面から CSV・Excel を取り込んでください（試すなら --demo で例の DB も作れます）。", "info");
    return;
  }
  if (!state.scenarios.length) {
    showMessage("試算がまだありません。「新しい試算」から作ってください。", "info");
    return;
  }
  await selectScenario(state.scenarios[0].scenario_id);
}

async function loadLists() {
  [state.versions, state.scenarios] = await Promise.all([api("GET", "/api/versions"), api("GET", "/api/scenarios")]);
  document.getElementById("version-select").replaceChildren(
    ...state.versions.map((v) => el("option", { value: v.version_id }, v.version_id)),
  );
  document.getElementById("scenario-select").replaceChildren(
    ...state.scenarios.map((s) => el("option", { value: s.scenario_id }, s.name)),
  );
}

async function selectScenario(id) {
  state.scenarioId = Number(id);
  state.selected = null;
  state.expanded = new Set();
  await reloadAll();
}

async function reloadAll() {
  state.childCache.clear();
  state.table = null;
  state.summary = await api("GET", `/api/scenarios/${state.scenarioId}`);
  const sc = state.summary.scenario;
  document.getElementById("scenario-select").value = String(sc.scenario_id);
  document.getElementById("version-select").value = sc.version_id;
  document.getElementById("head-select").value = sc.head;
  loadCodeList(sc.version_id);
  renderHistory();
  renderResult();
  await renderLeft();
  await renderDetail();
}

async function loadCodeList(versionId) {
  const items = await api("GET", `/api/items?version=${encodeURIComponent(versionId)}`);
  document.getElementById("code-list").replaceChildren(
    ...items.map((i) => el("option", { value: i.code }, i.name)),
  );
}

// ツリー -----------------------------------------------------------------

async function fetchChildren(path, code) {
  if (!state.childCache.has(path)) {
    const query = path.includes("/") ? viewQuery({ code }) : viewQuery();
    state.childCache.set(path, await api("GET", `/api/scenarios/${state.scenarioId}/tree?${query}`));
  }
  return state.childCache.get(path);
}

async function renderLeft() {
  const tree = document.getElementById("tree");
  const tableView = document.getElementById("table-view");
  tree.hidden = state.view !== "tree";
  tableView.hidden = state.view !== "table";
  document.getElementById("main-layout").classList.toggle("table-mode", state.view === "table");
  document.getElementById("left-title").textContent = state.view === "tree" ? "構成" : "構成（テーブル・見るだけ）";
  if (state.view === "tree") await renderTree();
  else await renderTable();
}

async function renderTree() {
  const tree = document.getElementById("tree");
  const rootData = await fetchChildren("", null);
  const root = rootData.node;
  const rootPath = root.code;
  document.getElementById("left-note").textContent =
    `${rootData.view.mode_label}・版 ${rootData.view.version_id}・${rootData.view.head_label}・参考値`;
  if (!state.expanded.size) state.expanded.add(rootPath);
  const rows = [];
  rows.push(nodeRow(root, rootPath, 0));
  await appendChildren(rows, root, rootPath, 1);
  tree.replaceChildren(...rows);
  if (!state.selected) {
    state.selected = { code: root.code, path: rootPath };
    markSelected();
    await renderDetail();
  } else {
    markSelected();
  }
}

async function appendChildren(rows, node, path, depth) {
  if (!node.has_children || !state.expanded.has(path)) return;
  const data = await fetchChildren(path, node.code);
  for (const [index, child] of data.children.entries()) {
    const childPath = `${path}/${child.balloon ?? ""}:${child.code}#${index}`;
    rows.push(nodeRow(child, childPath, depth));
    await appendChildren(rows, child, childPath, depth + 1);
  }
}

function roleTag(role) {
  if (!role) return null;
  const cls = role === "抜く" ? "tag remove" : role === "足す" ? "tag add" : "tag";
  return el("span", { class: cls }, role);
}

function nodeRow(node, path, depth) {
  const open = state.expanded.has(path);
  const twisty = node.has_children ? (open ? "▾" : "▸") : "";
  const qty = node.quantity === null ? null : `×${fmt(node.quantity)}`;
  const balloon = node.balloon ? `(${node.balloon})` : null;
  let costCell;
  if (node.cost === null) costCell = el("span", { class: "num missing" }, "未登録");
  else costCell = el("span", { class: "num" }, fmt(node.cost));
  const marks = [];
  if (node.cost !== null && node.incomplete) marks.push(el("span", { class: "tag warn", title: "足し上げた範囲に未登録がある" }, "一部未登録"));
  if (node.verdict && node.verdict !== "判定できない") marks.push(el("span", { class: "tag warn", title: "二重計上の判定（推定）" }, node.verdict));
  const row = el(
    "div",
    { class: "node", "data-path": path, style: `padding-left:${8 + depth * 18}px` },
    el(
      "span",
      { class: "label" },
      el("span", { class: "twisty", onclick: (e) => { e.stopPropagation(); toggle(path); } }, twisty),
      el("span", { class: "code" }, node.code),
      balloon && el("span", { class: "muted" }, balloon),
      qty && el("span", { class: "qty" }, qty),
      roleTag(node.role),
      el("span", { class: "name" }, node.name),
    ),
    el("span", {}, marks),
    el("span", { class: "cost" }, costCell),
  );
  row.addEventListener("click", () => guarded(() => select(node.code, path)));
  row.addEventListener("dblclick", () => node.has_children && toggle(path));
  return row;
}

function toggle(path) {
  if (state.expanded.has(path)) state.expanded.delete(path);
  else state.expanded.add(path);
  guarded(renderTree);
}

function markSelected() {
  for (const row of document.querySelectorAll(".node")) {
    row.classList.toggle("selected", state.selected && row.dataset.path === state.selected.path);
  }
}

async function select(code, path) {
  state.selected = { code, path };
  markSelected();
  await renderDetail();
}

// 詳細と操作 -------------------------------------------------------------

async function renderDetail() {
  const box = document.getElementById("detail");
  if (!state.scenarioId) return;
  if (!state.selected) {
    box.replaceChildren(el("p", { class: "muted" }, "左の構成から品目を選んでください。"));
    return;
  }
  const d = await api(
    "GET",
    `/api/scenarios/${state.scenarioId}/item?${viewQuery({ code: state.selected.code })}`,
  );
  const sources = [d.item_source && `品目 ${d.item_source}`, d.cost_source && `原価 ${d.cost_source}`]
    .filter(Boolean).join("・") || "登録なし";
  const kv = el(
    "dl",
    { class: "kv" },
    el("dt", {}, "区分・状態"), el("dd", {}, [d.kind, d.lifecycle].filter(Boolean).join("・") || "—"),
    el("dt", {}, "出どころ"), el("dd", {}, `${sources} ・ ${d.has_children ? "子部品あり" : "子部品なし"}`),
    el("dt", {}, "1台あたり"), el("dd", {}, d.usage === null ? "—" : `${fmt(d.usage)} 個`),
    el("dt", {}, "償却費"), el("dd", {}, `${fmt(d.amortization)}（計算から外す）`),
  );
  const heads = el(
    "table",
    { class: "small" },
    el("thead", {}, el("tr", {}, el("th", {}, "費目"), el("th", { class: "num" }, "登録値"), el("th", { class: "num" }, "計算値"), el("th", {}, "二重計上の判定"))),
    el(
      "tbody",
      {},
      d.heads.map((h) =>
        el(
          "tr",
          {},
          el("td", {}, h.label),
          el("td", {}, money(h.registered)),
          el("td", {}, money(h.calculated), h.calculated !== null && h.incomplete ? el("span", { class: "tag warn" }, "一部未登録") : null),
          el("td", {}, h.verdict ? `${h.verdict}${h.compared !== null ? `（比べた値 ${fmt(h.compared)}）` : ""}` : "—"),
        ),
      ),
    ),
  );
  box.replaceChildren(
    el("div", { class: "detail-title" }, `${d.code} ${d.name}`),
    kv,
    heads,
    actionsBlock(d),
  );
}

function actionsBlock(d) {
  if (state.mode !== "diff") {
    return el("p", { class: "muted" }, "一括のモードは土台の製品をそのまま見ます（打ち替えは差分のモードで）。");
  }
  if (d.is_root) {
    return el("p", { class: "muted" }, "試算の製品です。下の品目を選ぶと、替える・抜く・足すができます。");
  }
  const defaultQty = d.usage && !String(d.usage).startsWith("-") && !/^0*(\.0*)?$/.test(d.usage) ? d.usage : "1";
  const qty = el("input", { id: "act-qty", value: defaultQty, inputmode: "decimal" });
  const repl = el("input", { id: "act-replacement", list: "code-list", placeholder: "代わりの品番" });
  const addCode = el("input", { id: "act-add", list: "code-list", value: d.code });
  const send = (kind, extra) =>
    guarded(async () => {
      state.summary = await api("POST", `/api/scenarios/${state.scenarioId}/changes`, {
        kind,
        part: d.code,
        quantity: qty.value,
        ...extra,
      });
      await afterChange();
    });
  return el(
    "div",
    { class: "actions" },
    el("label", { for: "act-qty" }, "数量"), qty,
    el("button", { type: "button", onclick: () => send("REPLACE", { replacement: repl.value }) }, "替える"), repl,
    el("button", { type: "button", onclick: () => send("REMOVE") }, "抜く"), el("span", { class: "muted" }, `${d.code} を数量の分だけ抜く`),
    el("button", { type: "button", onclick: () => send("ADD", { part: addCode.value }) }, "足す"), addCode,
    d.in_root
      ? [el("button", { type: "button", onclick: () => send("SET_QUANTITY") }, "数量にする"), el("span", { class: "muted" }, "試算の製品の直下の数量を置き換える")]
      : null,
    el("p", { class: "muted note" }, "変更は試算の製品の直下に重ねて記録します（深い所の部品は「抜く＋足す」になります）。"),
  );
}

async function afterChange() {
  // 替えた・抜いた品目はツリーから消えるので、選びを戻す（ツリーなら試算の製品が選ばれる）
  state.selected = null;
  state.childCache.clear();
  state.table = null;
  renderHistory();
  renderResult();
  await renderLeft();
  await renderDetail();
}

// 履歴と結果 -------------------------------------------------------------

function premiseText(r) {
  const parts = [`版 ${r.version_id}`, r.head_label];
  if (r.volume !== null) parts.push(`台数 ${fmt(r.volume)}`);
  if (r.allocation) parts.push(`型費 ${r.allocation}`);
  return parts.join("・");
}

function renderHistory() {
  const s = state.summary;
  const list = document.getElementById("history");
  const items = [
    el(
      "li",
      { value: 0 },
      el("div", { class: "row" }, el("span", { class: "text" }, `${s.start.base} を土台に開始`), money(s.start.cost)),
      el("div", { class: "premise" }, `今の版 ${s.scenario.version_id}・${s.scenario.head_label}で計算`),
    ),
    ...s.history.map((r) =>
      el(
        "li",
        { value: r.sequence },
        el(
          "div",
          { class: "row" },
          el("span", { class: "text" }, r.text),
          el("span", { class: "num muted" }, `${fmt(r.cost_before)} → ${fmt(r.cost_after)}`),
          el("span", { class: `num ${signClass(r.difference)}` }, fmt(r.difference, { sign: true })),
        ),
        el("div", { class: "premise" }, `前提: ${premiseText(r)}・${r.recorded_at.replace("T", " ")}`),
      ),
    ),
  ];
  list.replaceChildren(...items);
  document.getElementById("undo").disabled = !s.history.length || state.mode !== "diff";
}

function renderResult() {
  const s = state.summary;
  const p = s.premises;
  const premise = [p.head_label, p.reference, p.amortization_excluded ? "償却費除外" : null, p.has_missing ? "一部未登録" : "未登録なし"]
    .filter(Boolean).join("・");
  const box = document.getElementById("result");
  const last = s.last;
  const line = last
    ? el(
      "div",
      { class: "result-line" },
      el("span", {}, el("span", { class: "label" }, "最後の変更"), last.text),
      el("span", {}, el("span", { class: "label" }, "変更前"), el("span", { class: "big num" }, fmt(last.cost_before))),
      el("span", {}, el("span", { class: "label" }, "変更後"), el("span", { class: "big num" }, fmt(last.cost_after))),
      el("span", {}, el("span", { class: "label" }, "差額"), el("span", { class: `big num ${signClass(last.difference)}` }, fmt(last.difference, { sign: true }))),
      el("span", { class: "muted" }, `（変更時の前提: 版 ${last.version_id}・${last.head_label}）`),
    )
    : el("div", { class: "result-line muted" }, "まだ変更がありません。");
  const now = el(
    "div",
    { class: "result-line" },
    el("span", {}, el("span", { class: "label" }, `今の ${s.scenario.product_code}（版 ${p.version_id}）`), el("span", { class: "big num" }, fmt(s.total))),
    s.volume ? el("span", { class: "muted" }, `年間台数 ${fmt(s.volume.annual)}（${s.volume.kind}）`) : null,
    s.missing.length ? el("span", { class: "missing" }, `未登録: ${s.missing.join("・")}`) : null,
    s.conflicts.length ? el("span", { class: "missing" }, `数量の食い違い ${s.conflicts.length} 件`) : null,
  );
  box.replaceChildren(el("div", { class: "result-head" }, `変更の結果（${premise}・版 ${p.version_id}）`), line, now);
}

// テーブル ---------------------------------------------------------------

async function renderTable() {
  if (!state.table) {
    state.table = await api("GET", `/api/scenarios/${state.scenarioId}/table?${viewQuery()}`);
  }
  const t = state.table;
  document.getElementById("left-note").textContent =
    `${t.view.mode_label}・版 ${t.view.version_id}・参考値・償却費除外`;
  renderColumnChecks();
  drawGrid();
}

function renderColumnChecks() {
  const box = document.getElementById("column-checks");
  box.replaceChildren(
    ...state.table.columns.map((c) => {
      const input = el("input", { type: "checkbox", checked: state.tableColumns.includes(c.key) });
      input.addEventListener("change", () => {
        const keys = new Set(state.tableColumns);
        if (input.checked) keys.add(c.key); else keys.delete(c.key);
        state.tableColumns = state.table.columns.map((col) => col.key).filter((k) => keys.has(k));
        drawGrid();
      });
      return el("label", {}, input, ` ${c.label}`);
    }),
  );
}

function visibleColumns() {
  return state.table.columns.filter((c) => state.tableColumns.includes(c.key));
}

function cellText(row, col) {
  const value = row[col.key];
  if (col.type === "money" || col.type === "number") return fmt(value);
  return value === null || value === undefined ? "" : String(value);
}

// 並べ替えのための比較だけで、原価の計算には使わない
function compareValues(a, b, type) {
  if (type === "money" || type === "number" || type === "int") {
    if (a === null || a === undefined) return b === null || b === undefined ? 0 : 1;
    if (b === null || b === undefined) return -1;
    return Number(a) - Number(b);
  }
  return String(a ?? "").localeCompare(String(b ?? ""), "ja");
}

function visibleRows() {
  const cols = visibleColumns();
  let rows = state.table.rows.filter((row) =>
    cols.every((c) => {
      const needle = (state.tableFilters[c.key] || "").trim();
      return !needle || cellText(row, c).toLowerCase().includes(needle.toLowerCase());
    }),
  );
  const { key, desc } = state.tableSort;
  if (key) {
    const col = state.table.columns.find((c) => c.key === key);
    rows = [...rows].sort((a, b) => compareValues(a[key], b[key], col.type) * (desc ? -1 : 1));
  }
  return rows;
}

function drawGrid() {
  const cols = visibleColumns();
  const grid = document.getElementById("grid");
  const heads = el(
    "tr",
    { class: "heads" },
    cols.map((c) => {
      const mark = state.tableSort.key === c.key ? (state.tableSort.desc ? " ▼" : " ▲") : "";
      return el("th", { onclick: () => sortBy(c.key), title: "クリックで並べ替え" }, c.label + mark);
    }),
  );
  const filters = el(
    "tr",
    { class: "filters" },
    cols.map((c) => {
      const input = el("input", { value: state.tableFilters[c.key] || "", placeholder: "絞り込み" });
      input.addEventListener("input", () => {
        state.tableFilters[c.key] = input.value;
        drawBody();
      });
      return el("th", {}, input);
    }),
  );
  grid.tHead.replaceChildren(heads, filters);
  drawBody();
}

function drawBody() {
  const cols = visibleColumns();
  const rows = visibleRows();
  const body = document.getElementById("grid").tBodies[0];
  body.replaceChildren(
    ...rows.map((row) =>
      el(
        "tr",
        {},
        cols.map((c) => {
          const numeric = c.type === "money" || c.type === "number" || c.type === "int";
          const missing = c.type === "money" && row[c.key] === null;
          return el("td", { class: `${numeric ? "num" : ""} ${missing ? "missing" : ""}` }, cellText(row, c));
        }),
      ),
    ),
  );
  document.getElementById("table-count").textContent = `${rows.length} / ${state.table.rows.length} 行`;
}

function sortBy(key) {
  if (state.tableSort.key === key) {
    if (state.tableSort.desc) state.tableSort = { key: null, desc: false };
    else state.tableSort.desc = true;
  } else {
    state.tableSort = { key, desc: false };
  }
  drawGrid();
}

async function exportXlsx() {
  const rows = visibleRows();
  const blob = await api("POST", `/api/scenarios/${state.scenarioId}/export`, {
    mode: state.mode,
    by_balloon: state.byBalloon ? "1" : "0",
    columns: visibleColumns().map((c) => c.key),
    row_ids: rows.map((r) => r.id),
  });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: `bom-cost-${state.summary.scenario.name}.xlsx` });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// 版の比較 ---------------------------------------------------------------

async function openCompare() {
  const dialog = document.getElementById("compare-dialog");
  const select = document.getElementById("compare-version");
  const current = state.summary.scenario.version_id;
  const others = state.versions.filter((v) => v.version_id !== current);
  select.replaceChildren(...state.versions.map((v) => el("option", { value: v.version_id }, v.version_id)));
  select.value = (others[0] || state.versions[0]).version_id;
  dialog.showModal();
  await drawCompare();
}

async function drawCompare() {
  const other = document.getElementById("compare-version").value;
  const c = await api("GET", `/api/scenarios/${state.scenarioId}/compare?version=${encodeURIComponent(other)}`);
  const table = el(
    "table",
    { class: "small" },
    el("thead", {}, el("tr", {}, el("th", {}, "品目"), el("th", { class: "num" }, "数量"), el("th", { class: "num" }, `版 ${c.version_a}`), el("th", { class: "num" }, `版 ${c.version_b}`), el("th", { class: "num" }, "差"))),
    el(
      "tbody",
      {},
      c.lines.map((l) =>
        el(
          "tr",
          {},
          el("td", {}, `${l.part} ${l.name} `, roleTag(l.role)),
          el("td", { class: "num" }, fmt(l.quantity)),
          el("td", {}, money(l.amount_a)),
          el("td", {}, money(l.amount_b)),
          el("td", { class: `num ${signClass(l.difference)}` }, fmt(l.difference, { sign: true, missing: "—" })),
        ),
      ),
      el(
        "tr",
        {},
        el("th", {}, c.product_code),
        el("td", {}),
        el("th", {}, money(c.total_a)),
        el("th", {}, money(c.total_b)),
        el("th", { class: `num ${signClass(c.difference)}` }, fmt(c.difference, { sign: true, missing: "—" })),
      ),
    ),
  );
  document.getElementById("compare-body").replaceChildren(
    table,
    el("p", { class: "muted" }, `${c.head_label}・参考値・償却費除外。版を切り替えて計算し直すには、上の帯の「版」を選び直します。`),
  );
}

// 操作の配線 -------------------------------------------------------------

function setSeg(id, attr, value) {
  for (const b of document.querySelectorAll(`#${id} button`)) b.classList.toggle("on", b.dataset[attr] === value);
}

function wire() {
  document.querySelectorAll("#mode-seg button").forEach((b) =>
    b.addEventListener("click", () => guarded(async () => {
      state.mode = b.dataset.mode;
      setSeg("mode-seg", "mode", state.mode);
      state.selected = null;
      state.expanded = new Set();
      await reloadAll();
    })),
  );
  document.querySelectorAll("#balloon-seg button").forEach((b) =>
    b.addEventListener("click", () => guarded(async () => {
      state.byBalloon = b.dataset.balloon === "1";
      setSeg("balloon-seg", "balloon", b.dataset.balloon);
      state.expanded = new Set();
      state.selected = null;
      state.childCache.clear();
      state.table = null;
      await renderLeft();
    })),
  );
  document.querySelectorAll("#view-seg button").forEach((b) =>
    b.addEventListener("click", () => guarded(async () => {
      state.view = b.dataset.view;
      setSeg("view-seg", "view", state.view);
      await renderLeft();
    })),
  );
  document.getElementById("version-select").addEventListener("change", (e) => guarded(async () => {
    state.summary = await api("PUT", `/api/scenarios/${state.scenarioId}`, { version_id: e.target.value });
    await reloadAll();
    showMessage(`版 ${e.target.value} の原価で計算し直しました（履歴の各行は変更した時の前提のまま残ります）。`, "info");
  }));
  document.getElementById("head-select").addEventListener("change", (e) => guarded(async () => {
    state.summary = await api("PUT", `/api/scenarios/${state.scenarioId}`, { head: e.target.value });
    await reloadAll();
  }));
  document.getElementById("scenario-select").addEventListener("change", (e) => guarded(() => selectScenario(e.target.value)));
  document.getElementById("undo").addEventListener("click", () => guarded(async () => {
    state.summary = await api("POST", `/api/scenarios/${state.scenarioId}/undo`, {});
    await afterChange();
  }));
  document.getElementById("export-xlsx").addEventListener("click", () => guarded(exportXlsx));
  document.getElementById("open-compare").addEventListener("click", () => guarded(openCompare));
  document.getElementById("compare-version").addEventListener("change", () => guarded(drawCompare));
  document.getElementById("compare-close").addEventListener("click", () => document.getElementById("compare-dialog").close());

  const dialog = document.getElementById("new-dialog");
  document.getElementById("new-scenario").addEventListener("click", () => {
    const version = document.getElementById("version-select").value;
    const head = document.getElementById("head-select");
    document.getElementById("new-premise").textContent =
      `版 ${version || "—"}・${head.options[head.selectedIndex]?.text || "材料費"}`;
    if (version) loadCodeList(version);
    dialog.showModal();
  });
  document.getElementById("new-cancel").addEventListener("click", () => dialog.close());
  document.getElementById("new-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = new FormData(e.target);
    guarded(async () => {
      const created = await api("POST", "/api/scenarios", {
        name: form.get("name"),
        base: form.get("base"),
        product_code: form.get("product_code"),
        version_id: document.getElementById("version-select").value,
        head: document.getElementById("head-select").value,
      });
      dialog.close();
      e.target.reset();
      await loadLists();
      await selectScenario(created.scenario.scenario_id);
    });
  });
}

wire();
guarded(init);
