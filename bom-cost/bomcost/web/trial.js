// ②構成変更: 検討案を開き、その検討案の試算がある製品を並べて、行の中に開いた版の構成で置換・削除・追加、
// 部品の名前で絞った製品×部品の行を置換（選んだ所をまとめても）する。原価は③原価比較で見る。
// 差は製造原価（TRIAL_HEAD・group.js）。app.js・screens.js・compare.js の関数を使う。
"use strict";

let trialOpened = null; // 開いた { version, name }。選び直しても［開く］までは前の検討案へ変更を送る
let trialPending = null; // ①対象製品の画面から開く検討案

// ①対象製品の画面の検討案の行から、その検討案を開いた状態で移る
async function openTrial(version, name) {
  trialPending = { version, name };
  await showScreen("trial");
}

async function enterTrial() {
  const version = document.getElementById("trial-version");
  const pending = trialPending;
  trialPending = null;
  versionOptions(version, { value: pending?.version || version.value || state.summary?.scenario.version_id });
  await loadTrialNames(pending?.name);
  await loadCodes(version.value);
  trialOpen.clear();  // 開いたときは製品の行を全部閉じて並べる
  if (pending) await openSelectedTrial();
  else if (trialOpened && trialOpened.version === version.value) await drawTrial();
}

async function loadTrialNames(value) {
  const version = document.getElementById("trial-version").value;
  const trials = version ? await api("GET", `/api/group/trials?${qs({ version })}`) : [];
  options(document.getElementById("trial-name"), trials.map((t) => [t.name, `${t.name}（${t.count} 製品）`]), { value });
}

async function openSelectedTrial() {
  const version = document.getElementById("trial-version").value;
  const name = document.getElementById("trial-name").value;
  if (!version) throw new Error("版が空");
  if (!name) throw new Error("検討案を選ぶ");
  trialOpened = { version, name };
  trialOpen.clear();
  trialChecked.clear();
  await drawTrial();
}

async function trialChange(product, change) {
  const t = trialOpened;
  await api("POST", "/api/group/changes", { version_id: t.version, name: t.name, head: TRIAL_HEAD, product, ...change });
  await loadLists();
  await drawTrial();
}

// 1回の操作（変更の一覧の1行）を取り消す。最後の操作でなくてもよい
async function trialUndo(change) {
  await api("POST", "/api/group/changes/undo", { scenario_id: change.scenario_id, sequence: change.sequence });
  await drawTrial();
}

// 部品の絞り込みでチェックした製品×部品を、まとめて同じ部品に置換する（1つでも通らなければ何も入らない）
async function trialBulk(rows, change) {
  const items = rows.filter((r) => trialChecked.has(checkKey(r))).map((r) => ({ product: r.product, part: r.part }));
  if (!items.length) throw new Error("置換する所を選ぶ");
  const t = trialOpened;
  await api("POST", "/api/group/changes/bulk", { version_id: t.version, name: t.name, head: TRIAL_HEAD, items, ...change });
  trialChecked.clear();
  await loadLists();
  await drawTrial();
}

// ③原価比較へ、今の版と検討案を選んだ状態で移る（compare.js の入口を使う）
async function trialToCompare() {
  const t = trialOpened;
  await showScreen("compare");
  const version = document.getElementById("cmp-version");
  if (version.value !== t.version) {
    version.value = t.version;
    await loadCompareNames();
  }
  for (const box of document.querySelectorAll("#cmp-names input")) box.checked = box.value === t.name;
  await drawCompareTable();
}

// 製品の行の下に開く版の構成。開いた所は道筋（製品/風船:品番#版の並び順）で持ち、表を取り直しても開いたままにする
const trialOpen = new Set();
const trialChecked = new Set(); // 部品の絞り込みでチェックした「製品\t部品」
let trialSort = { key: null, desc: false }; // 構成の表の並べ替え（# は風船番号）

const checkKey = (r) => `${r.product}\t${r.part}`;
const trialCode = (code) => codeSpan(trialOpened?.version, code);

function toggleTrial(path) {
  if (trialOpen.has(path)) trialOpen.delete(path);
  else trialOpen.add(path);
  guarded(drawTrial);
}

function sortTrial(key) {
  trialSort = trialSort.key === key ? { key, desc: !trialSort.desc } : { key, desc: false };
  guarded(drawTrial);
}

function trialTree(t, product, code) {
  return api("GET", `/api/group/tree?${qs({ version: t.version, head: TRIAL_HEAD, product, code, by_balloon: "1" })}`);
}

function twistyCell(path, canOpen) {
  return el("span", { class: "group-twisty", onclick: () => canOpen && toggleTrial(path) },
    canOpen ? (trialOpen.has(path) ? "▼" : "▶") : "");
}

// 同じ親の子の中で並べる。風船番号は数として比べ、無い行は向きによらず後ろ
function sortedChildren(children) {
  const list = children.map((node, index) => ({ node, index }));
  const { key, desc } = trialSort;
  if (!key) return list;
  const dir = desc ? -1 : 1;
  const number = (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)) ? null : Number(v));
  const value = { balloon: (n) => number(n.balloon), code: (n) => n.code, name: (n) => n.name || null,
    quantity: (n) => number(n.quantity) }[key];
  return list.sort((a, b) => {
    const x = value(a.node);
    const y = value(b.node);
    if (x === null || y === null) return x === y ? a.index - b.index : x === null ? 1 : -1;
    const c = typeof x === "number" ? x - y : String(x).localeCompare(String(y), "ja");
    return c ? c * dir : a.index - b.index;
  });
}

// 行の「変更」ボタンと、押すとその行の下に開く欄（操作を選ぶと、その操作の欄だけが使える）。
// 開くのは同時に1つ。表を描き直すと閉じる
let trialForm = null;

const codeInput = (value = "") => el("input", { list: "code-list-2", placeholder: "品番", value, size: 10 });
const qtyInput = (value, placeholder) => el("input", { value, placeholder, inputmode: "decimal", size: 4 });

// 操作: { label, text: 文の部品, inputs: 文の中の欄, change: () => API へ送る変更, code?: 品番の欄（無ければ登録して使う） }
const replaceOp = (part, qty) => {
  const code = codeInput();
  const q = qtyInput(qty);
  return { label: "置換", text: [`${part} を `, code, " に置換　員数 ", q], inputs: [code, q], code,
    change: () => ({ kind: "REPLACE", part, replacement: code.value.trim(), quantity: q.value }) };
};
// 版の構成の場所（親・風船番号）の置換。代わりの部品は「+ 行を足す」で増やせ、行ごとに無い品番を登録できる
const placeReplaceOp = (parent, balloon, part, qty) => {
  const op = { label: "置換", entries: [], onInput: null };
  const list = el("div", { class: "change-replacements" });
  const addRow = (value) => {
    const entry = { input: codeInput(), qty: qtyInput(value) };
    entry.reg = registerPanel(entry.input);
    entry.input.addEventListener("input", () => op.onInput?.());
    op.entries.push(entry);
    list.append(el("div", { class: "change-line" }, entry.input, " × ", entry.qty), entry.reg.panel);
  };
  const more = el("button", { type: "button", onclick: () => { addRow("1"); op.onInput?.(); } }, "+ 行を足す");
  addRow(qty);
  op.text = [`${balloon ? `#${balloon} の ` : ""}${part} を`, list, more];
  Object.defineProperty(op, "inputs", { get: () => [...op.entries.flatMap((e) => [e.input, e.qty]), more] });
  op.regs = () => op.entries;
  op.change = () => ({ kind: "REPLACE", part, parent, balloon,
    replacements: op.entries.filter((e) => e.input.value.trim()).map((e) => ({ code: e.input.value.trim(), quantity: e.qty.value })) });
  return op;
};
const removeOp = (part, qty) => ({ label: "削除", text: [`${part} を構成から削除`], inputs: [],
  change: () => ({ kind: "REMOVE", part, quantity: qty }) });
// 版の構成の場所（親・風船番号）の員数を変える
const quantityOp = (parent, balloon, part, qty) => {
  const q = qtyInput(qty);
  return { label: "員数変更", text: [`${balloon ? `#${balloon} の ` : ""}${part} の員数を `, q, " にする"], inputs: [q],
    change: () => ({ kind: "SET_QUANTITY", part, parent, balloon, quantity: q.value }) };
};
// 追加は、どの行から足しても製品（試算の根）の直下へ入る（scenario.py の _apply）
const addOp = (product, code = "") => {
  const c = codeInput(code);
  const q = qtyInput("1");
  return { label: "追加", text: [`${product} の直下に `, c, " を ", q, " 個追加"], inputs: [c, q], code: c,
    change: () => ({ kind: "ADD", part: c.value.trim(), quantity: q.value }) };
};
// 選んだ所をまとめて置換。員数が空なら場所ごとに元の員数
const bulkReplaceOp = () => {
  const code = codeInput();
  const q = qtyInput("", "元の員数");
  return { label: "置換", text: ["選んだ所を ", code, " に置換　員数 ", q], inputs: [code, q], code,
    change: () => ({ replacement: code.value.trim(), quantity: q.value.trim() }) };
};

// 品番の候補（loadCodes が版の品番を入れた datalist）に無い品番か。空は登録の対象にしない
function unknownCode(input) {
  const code = input.value.trim();
  return Boolean(code) && ![...document.getElementById("code-list-2").options].some((o) => o.value === code);
}

// 品番が無い時に出す、部品として登録する欄（名称と費目）
function registerPanel(codeField) {
  const name = el("input", { placeholder: "名称", size: 12 });
  const costs = state.meta.heads.map((h) => [h.id, el("input", { inputmode: "decimal", size: 8 })]);
  const panel = el("div", { class: "change-register", hidden: true },
    el("label", {}, "名称 ", name),
    state.meta.heads.map((h, i) => el("label", {}, `${h.label} `, costs[i][1])));
  const body = () => ({ role: "part", code: codeField.value.trim(), name: name.value,
    costs: Object.fromEntries(costs.map(([id, input]) => [id, input.value])) });
  return { panel, inputs: [name, ...costs.map(([, input]) => input)], body };
}

function closeChangeForm() {
  trialForm?.remove();
  trialForm = null;
}

// send は実行で送る先（省けばその製品の変更1件）
function changeButton(product, makeOps, { label = "変更", send } = {}) {
  const button = el("button", { type: "button" }, label);
  button.addEventListener("click", () => openChangeForm(button.closest("tr"), product, makeOps(), send));
  return button;
}

function openChangeForm(row, product, ops, send = (change) => trialChange(product, change)) {
  const same = trialForm?.previousElementSibling === row;
  closeChangeForm();
  if (same) return;
  // 品番の欄と登録の欄の組。場所の置換は行ごと（op.regs）、ほかは op.code の1つを行の下に出す
  const lines = ops.map((op, i) => ({ op, radio: el("input", { type: "radio", name: "trial-change-kind", checked: i === 0 }),
    fixed: op.code ? [{ input: op.code, reg: registerPanel(op.code) }] : [] }));
  const regsOf = (line) => (line.op.regs ? line.op.regs() : line.fixed);
  const runButton = el("button", { type: "button", class: "primary" }, "実行");
  const toRegister = (line) => regsOf(line).filter((r) => unknownCode(r.input));
  const sync = () => {
    for (const line of lines) {
      line.op.inputs.forEach((x) => { x.disabled = !line.radio.checked; });
      regsOf(line).forEach((r) => { r.reg.panel.hidden = !(line.radio.checked && unknownCode(r.input)); });
    }
    const active = lines.find((l) => l.radio.checked);
    runButton.textContent = toRegister(active).length ? `登録して${active.op.label}` : "実行";
  };
  lines.forEach(({ op, radio }) => {
    radio.addEventListener("change", sync);
    op.code?.addEventListener("input", sync);
    op.onInput = sync;
  });
  sync();
  // 品番が無ければ部品として登録 → 品番の候補を読み直す → 変更。変更が通らなくても登録した部品は残す
  runButton.addEventListener("click", () => guarded(async () => {
    const line = lines.find((l) => l.radio.checked);
    const regs = toRegister(line);
    if (regs.length) {
      for (const r of regs) await api("PUT", "/api/manual/items", { ...r.reg.body(), version: trialOpened.version });
      await loadCodes(trialOpened.version);
      sync();
    }
    await send(line.op.change());
  }));
  const colspan = [...row.children].reduce((n, td) => n + td.colSpan, 0);
  trialForm = el("tr", { class: "change-form" },
    el("td", { colspan },
      lines.flatMap(({ op, radio, fixed }) => [
        el("div", { class: "change-line" }, el("label", {}, radio, " ", op.label), el(op.regs ? "div" : "span", {}, op.text)),
        fixed.map((r) => r.reg.panel)]),
      el("div", { class: "change-buttons" },
        runButton,
        el("button", { type: "button", onclick: closeChangeForm }, "やめる"))));
  row.after(trialForm);
}

// 変更の一覧の1行の文（置換・場所の員数変更は API の文のまま）
function changeText(c) {
  if (c.kind === "REMOVE") return `削除 ${c.part}`;
  if (c.kind === "ADD") return `追加 ${c.part} ×${fmt(c.quantity)}`;
  if (c.kind === "SET_QUANTITY" && c.parent === null) return `員数変更 ${c.part} → ${fmt(c.quantity)}`;
  return c.text;
}

const undoButton = (change) => el("button", { type: "button", onclick: () => guarded(() => trialUndo(change)) }, "取り消す");
const CHANGE_CLASS = { REPLACE: "chg-replace", REMOVE: "chg-remove", ADD: "chg-add", SET_QUANTITY: "chg-qty" };

// 構成の行に当たる変更: その場所の置換・員数変更、なければその品番の削除・場所なしの置換
function rowChange(changes, node) {
  return changes.find((c) => (c.kind === "REPLACE" || c.kind === "SET_QUANTITY") && c.parent !== null
      && c.parent === node.parent && (c.balloon ?? null) === (node.balloon ?? null) && c.part === node.code)
    ?? changes.find((c) => (c.kind === "REMOVE" || (c.kind === "REPLACE" && c.parent === null)) && c.part === node.code);
}

// 構成の行の［変更］（置換・削除・員数変更・追加）か、変えた行なら［取り消す］
function rowButton(ctx, node, change) {
  if (change) return undoButton(change);
  const usage = node.usage && !String(node.usage).startsWith("-") && !/^0*(\.0*)?$/.test(node.usage) ? node.usage : "1";
  const placeQty = node.quantity && !String(node.quantity).startsWith("-") ? node.quantity : "1";
  return changeButton(ctx.product, () => [placeReplaceOp(node.parent, node.balloon, node.code, placeQty),
    removeOp(node.code, usage), quantityOp(node.parent, node.balloon, node.code, placeQty), addOp(ctx.product, node.code)]);
}

function trialNodeRow(ctx, node, path, depth) {
  const change = rowChange(ctx.changes, node);
  let code = trialCode(node.code);
  let name = node.name;
  let specification = node.specification || "";
  let quantity = fmt(node.quantity, { missing: "" });
  if (change?.kind === "REPLACE") {
    code = [trialCode(node.code), "→", change.replacements.flatMap((r, i) => [i ? "・" : null, trialCode(r.code)])];
    specification = `${specification}→${change.replacements.map((r) => change.replacement_specifications?.[r.code] || "").join("・")}`;
    name = `${node.name}→${change.replacements.map((r) => r.name || r.code).join("・")}`;
  } else if (change?.kind === "SET_QUANTITY") {
    quantity = `${quantity}→${fmt(change.quantity)}`;
  } else if (change) {
    code = [el("s", { title: codeTitle(trialOpened?.version, node.code) }, node.code), " (削除)"];
    name = el("s", {}, node.name);
  }
  return el(
    "tr",
    { class: `group-node ${change ? `trial-changed ${CHANGE_CLASS[change.kind]}` : ""}` },
    el("td", { class: "num" }, node.balloon ?? ""),
    el("td", { style: `padding-left:${6 + depth * 18}px` }, twistyCell(path, node.has_children), code),
    el("td", {}, name), el("td", {}, specification),
    el("td", { class: "num" }, quantity),
    el("td", {}, node.kind || ""),
    el("td", {}, rowButton(ctx, node, change)),
  );
}

// 版の構成の子を並べ（並べ替えは同じ親の子の中）、開いた所はたどる。rowsOf は1つの子の行を作る
async function addTrialChildren(rows, ctx, data, path, depth, rowsOf, gone = false) {
  for (const { node, index } of sortedChildren(data.children)) {
    const childPath = `${path}/${node.balloon ?? ""}:${node.code}#${index}`;
    const change = gone ? null : rowChange(ctx.changes, node);
    rows.push(...[rowsOf(ctx, node, childPath, depth, gone)].flat());
    if (node.has_children && trialOpen.has(childPath)) {
      // 置換・削除した組立品の下は変更後に残らない
      const below = gone || change?.kind === "REPLACE" || change?.kind === "REMOVE";
      await addTrialChildren(rows, ctx, await trialTree(ctx.t, ctx.product, node.code), childPath, depth + 1, rowsOf, below);
    }
  }
}

// 足した部品（と場所の無い員数変更）は版の構成に無いので、構成の後ろに出す
const rootExtras = (changes) => changes.filter((c) => c.kind === "ADD" || (c.kind === "SET_QUANTITY" && c.parent === null));

function addedRows(changes) {
  return rootExtras(changes).map((c) => el(
    "tr",
    { class: `group-node trial-changed ${CHANGE_CLASS[c.kind]}` },
    el("td", {}, ""),
    el("td", {}, el("span", { class: "trial-plus" }, c.kind === "ADD" ? "＋ " : "員数 "), trialCode(c.part)),
    el("td", {}, c.part_name ?? ""), el("td", {}, c.part_specification || ""),
    el("td", { class: "num" }, fmt(c.quantity)),
    el("td", {}, c.part_kind ?? ""),
    el("td", {}, undoButton(c)),
  ));
}

// 左右: 左に現行、右に変更後。片側にしか無い行は反対側を空ける
const PAIR_COLS = 6;
const emptyCells = () => Array.from({ length: PAIR_COLS }, (_, i) => el("td", { class: i === 0 ? "pair-split" : "" }, ""));

function pairCells(node, depth, twisty, first) {
  return [
    el("td", { class: `num ${first ? "pair-split" : ""}` }, node.balloon ?? ""),
    el("td", { style: `padding-left:${6 + depth * 18}px` }, twisty, trialCode(node.code)),
    el("td", {}, node.name ?? ""), el("td", {}, node.specification || ""),
    el("td", { class: "num" }, fmt(node.quantity, { missing: "" })),
    el("td", {}, node.kind || ""),
  ];
}

function trialPairRows(ctx, node, path, depth, gone) {
  const left = pairCells(node, depth, twistyCell(path, node.has_children), false);
  if (gone) return el("tr", { class: "group-node chg-remove" }, left, emptyCells(), el("td", {}, ""));
  const change = rowChange(ctx.changes, node);
  const cls = `group-node ${change ? CHANGE_CLASS[change.kind] : ""}`;
  const button = el("td", {}, rowButton(ctx, node, change));
  if (!change) return el("tr", { class: cls }, left, pairCells(node, depth, null, true), button);
  if (change.kind === "REMOVE") return el("tr", { class: cls }, left, emptyCells(), button);
  if (change.kind === "SET_QUANTITY") {
    return el("tr", { class: cls }, left, pairCells({ ...node, quantity: change.quantity }, depth, null, true), button);
  }
  // 置換は代わりの部品ごとに右へ1行（2つ目からは左を空ける）
  return change.replacements.map((r, i) => el("tr", { class: cls },
    i === 0 ? left : Array.from({ length: PAIR_COLS }, () => el("td", {}, "")),
    pairCells({ balloon: node.balloon, code: r.code, name: r.name, specification: change.replacement_specifications?.[r.code], quantity: r.quantity, kind: r.kind }, depth, null, true),
    i === 0 ? button : el("td", {}, "")));
}

function addedPairRows(changes) {
  return rootExtras(changes).map((c) => el("tr", { class: `group-node ${CHANGE_CLASS[c.kind]}` },
    Array.from({ length: PAIR_COLS }, () => el("td", {}, "")),
    pairCells({ balloon: null, code: c.part, name: c.part_name, specification: c.part_specification, quantity: c.quantity, kind: c.part_kind }, 0, null, true),
    el("td", {}, undoButton(c))));
}

function sortTh(key, label, num, cls = "") {
  const mark = trialSort.key === key ? (trialSort.desc ? "▼" : "▲") : "";
  return el("th", { class: `sortable ${num ? "num" : ""} ${cls}`, onclick: () => sortTrial(key) }, `${label}${mark}`);
}

const structureHeads = (cls = "") => [sortTh("balloon", "#", true, cls), sortTh("code", "品番"), sortTh("name", "名称"), "記号・寸法",
  sortTh("quantity", "員数", true), "区分"];

let trialLayout = "list"; // 開いた製品の構成の見せ方（list＝一覧・pair＝左右）。画面を開いている間は製品をまたいで覚える

function layoutSeg() {
  const button = (value, label) => el("button", { type: "button", class: trialLayout === value ? "on" : "",
    onclick: () => { trialLayout = value; guarded(drawTrial); } }, label);
  return el("div", { class: "seg trial-layout" }, button("list", "一覧"), button("pair", "左右"));
}

// 開いた製品の中身: この製品の変更（1回の操作ごと）と、変えた所に印を付けた版の構成（一覧か左右）
async function trialProductPanel(t, product, changes) {
  const root = await trialTree(t, product);
  const ctx = { t, product, changes };
  const rows = [];
  const pair = trialLayout === "pair";
  await addTrialChildren(rows, ctx, root, product, 0, pair ? trialPairRows : trialNodeRow);
  rows.push(...(pair ? addedPairRows(changes) : addedRows(changes)));
  const changeRows = changes.map((c) => el("tr", { class: CHANGE_CLASS[c.kind] },
    el("td", {}, changeText(c)),
    el("td", { class: `num ${signClass(c.difference)}` }, fmt(c.difference, { sign: true, missing: "—" })),
    el("td", {}, undoButton(c))));
  const addRow = el("tr", {}, el("td", { colspan: 3 }, changeButton(product, () => [addOp(product)], { label: "追加" })));
  const heads = pair ? [...structureHeads(), ...structureHeads("pair-split"), ""] : [...structureHeads(), ""];
  const sides = pair ? el("tr", { class: "pair-sides" },
    el("th", { colspan: PAIR_COLS }, "現行"), el("th", { colspan: PAIR_COLS, class: "pair-split" }, "変更後"), el("th", {}, "")) : null;
  const structure = table(heads, rows.length ? rows : [el("tr", {}, el("td", { colspan: heads.length, class: "muted" }, "なし"))]);
  if (sides) structure.querySelector("thead").prepend(sides);
  return el("tr", { class: "trial-open" }, el("td", { colspan: 4 },
    el("div", { class: "trial-changes" },
      el("div", { class: "trial-changes-head" }, "この製品の変更"),
      el("table", { class: "small" }, el("tbody", {},
        changeRows.length ? changeRows : el("tr", {}, el("td", { colspan: 3, class: "muted" }, "なし")), addRow))),
    layoutSeg(),
    el("div", { class: "table-scroll" }, structure)));
}

function partBlock(parts) {
  const all = el("input", { type: "checkbox", title: "全部",
    checked: parts.rows.length > 0 && parts.rows.every((r) => trialChecked.has(checkKey(r))) });
  const boxes = parts.rows.map((r) => {
    const box = el("input", { type: "checkbox", checked: trialChecked.has(checkKey(r)) });
    box.addEventListener("change", () => { if (box.checked) trialChecked.add(checkKey(r)); else trialChecked.delete(checkKey(r)); });
    return box;
  });
  all.addEventListener("change", () => boxes.forEach((box, i) => {
    box.checked = all.checked;
    if (all.checked) trialChecked.add(checkKey(parts.rows[i])); else trialChecked.delete(checkKey(parts.rows[i]));
  }));
  const partRows = parts.rows.map((r, i) => el(
    "tr",
    {},
    el("td", {}, boxes[i]),
    el("td", {}, trialCode(r.product), ` ${r.product_name}`),
    el("td", {}, trialCode(r.part)),
    el("td", {}, r.part_name), el("td", {}, r.part_specification),
    numTd(r.usage),
    el("td", {}, r.replaced_by.length ? r.replaced_by.map((c) => el("span", { class: "tag add" }, `→ ${c}`)) : "—"),
    el("td", {}, changeButton(r.product, () => [replaceOp(r.part, r.usage)])),
  ));
  const bulk = el("tr", {}, el("td", { colspan: 8 },
    changeButton(null, () => [bulkReplaceOp()], { label: "選んだ所を置換", send: (change) => trialBulk(parts.rows, change) })));
  return [
    el("h3", {}, `部品「${parts.part}」（${parts.rows.length} 件${parts.truncated ? "（上限）" : ""}）`),
    table([el("th", {}, all), "製品", "部品", "名称", "記号・寸法", numTh("使用数（個/台）"), "置換先", ""],
      partRows.length ? [...partRows, bulk] : [el("tr", {}, el("td", { colspan: 8, class: "muted" }, "なし"))]),
  ];
}

async function drawTrial() {
  const t = trialOpened;
  if (!t) throw new Error("検討案を開く");
  const part = document.getElementById("trial-part").value.trim();
  const base = { version: t.version, name: t.name, trial: "1" };
  const [p, parts, changes] = await Promise.all([
    api("GET", `/api/group/products?${qs({ ...base, head: TRIAL_HEAD })}`),
    part ? api("GET", `/api/group/parts?${qs({ ...base, part })}`) : null,
    api("GET", `/api/group/changes?${qs({ version: t.version, name: t.name, head: TRIAL_HEAD })}`),
  ]);
  trialForm = null;  // 表を描き直すと開いていた欄は消える
  const byProduct = new Map();
  for (const c of changes.rows) byProduct.set(c.product, [...(byProduct.get(c.product) ?? []), c]);
  const productRows = [];
  let count = 0;
  for (const x of p.products) {
    const own = byProduct.get(x.code) ?? [];
    count += own.length;
    productRows.push(el(
      "tr",
      {},
      el("td", {}, twistyCell(x.code, true), trialCode(x.code)),
      el("td", {}, x.name),
      el("td", { class: "num" }, String(own.length)),
      el("td", { class: `num ${signClass(x.difference)}`,
        title: own.map((c) => `${c.text}　${fmt(c.difference, { sign: true, missing: "—" })}`).join("\n") },
        fmt(x.difference, { sign: true, missing: "—" })),
    ));
    if (trialOpen.has(x.code)) productRows.push(await trialProductPanel(t, x.code, own));
  }
  const total = el("tr", { class: "trial-total" },
    el("th", { colspan: 2 }, `合計${p.missing_count ? `（未登録 ${p.missing_count} 製品を除く）` : ""}`),
    el("th", { class: "num" }, String(count)),
    el("th", { class: `num ${signClass(p.difference)}` }, fmt(p.difference, { sign: true, missing: "—" })));
  const heads = ["製品", "名称", numTh("変更"), numTh(`差（${p.head_label}）`)];
  document.getElementById("trial-body").replaceChildren(
    el("p", { class: "premise-line" }, `前提: ${versionLabel(p.version_id)}・検討案 ${p.name}・${p.head_label}・参考値`),
    el("h3", {}, `製品（${countText(p.count, p.products.length, p.truncated)}）`),
    table(heads, productRows.length ? [...productRows, total]
      : [el("tr", {}, el("td", { colspan: heads.length, class: "muted" }, "なし"))]),
    ...(parts ? partBlock(parts) : []),
    el("div", { class: "trial-foot" },
      el("button", { type: "button", class: "primary", onclick: () => guarded(trialToCompare) }, "③原価比較で見る →")),
  );
}

function wireTrial() {
  document.getElementById("trial-open").addEventListener("click", () => guarded(openSelectedTrial));
  document.getElementById("trial-part-show").addEventListener("click", () => guarded(drawTrial));
  document.getElementById("trial-part").addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawTrial));
  document.getElementById("trial-version").addEventListener("change", (e) => guarded(async () => {
    await loadTrialNames();
    await loadCodes(e.target.value);
  }));
}

wireTrial();
