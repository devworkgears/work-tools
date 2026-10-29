// 試作品: 試作キーを開き、その試作キーの試算がある製品を並べて、行の中に開いた構成で替える・抜く・足す、
// 部品の名前で絞った製品×部品の行を替える。原価は製造原価（TRIAL_HEAD・group.js）。app.js・screens.js の関数を使う。
"use strict";

let trialOpened = null; // 開いた { version, name }。選び直しても［開く］までは前の試作キーへ変更を送る
let trialPending = null; // 製品群の画面から開く試作キー

// 製品群の画面の「試作キー」の行から、その試作キーを開いた状態で移る
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
  trialScenario.clear();
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
  if (!name) throw new Error("試作キーを選ぶ");
  trialOpened = { version, name };
  trialOpen.clear();
  trialScenario.clear();
  await drawTrial();
}

async function trialChange(product, change) {
  const t = trialOpened;
  await api("POST", "/api/group/changes", { version_id: t.version, name: t.name, head: TRIAL_HEAD, product, ...change });
  await loadLists();
  await drawTrial();
}

async function trialUndo(scenarioId) {
  await api("POST", `/api/scenarios/${scenarioId}/undo`, {});
  await drawTrial();
}

// 製品の行の下に開く構成。行の形は「製品から」のツリーと同じ（API が試作キーの試算の構成を返す）。
// 開いた所は道筋（製品/風船:品番#並び順/…）で持ち、表を取り直しても開いたままにする
const trialOpen = new Set();
const trialScenario = new Map(); // 製品 → 前に開いた時の試算の ID（無ければ null）

function toggleTrial(path) {
  if (trialOpen.has(path)) trialOpen.delete(path);
  else {
    trialOpen.add(path);
    // 製品の行を開いたら、試作品の「土台」の行も開いて部品まで見せる（版の構成なら当たる行が無く効かない）
    if (!path.includes("/")) trialOpen.add(`${path}/:${path}#0`);
  }
  guarded(drawTrial);
}

function trialTree(t, product, code) {
  return api("GET", `/api/group/tree?${qs({ version: t.version, head: TRIAL_HEAD, name: t.name, product, code })}`);
}

// 試算ができると、版の構成は試算の根の直下の「土台」の行（並び順 0）の下へ1段下がる。開いていた所もそこへ移す
function moveUnderBase(product) {
  const prefix = `${product}/`;
  const base = `${product}/:${product}#0`;
  for (const path of [...trialOpen]) {
    if (!path.startsWith(prefix)) continue;
    trialOpen.delete(path);
    trialOpen.add(`${base}/${path.slice(prefix.length)}`);
  }
  trialOpen.add(base);
}

function twistyCell(path, canOpen) {
  return el("span", { class: "group-twisty", onclick: () => canOpen && toggleTrial(path) },
    canOpen ? (trialOpen.has(path) ? "▼" : "▶") : "");
}

// 行の「変更」ボタンと、押すとその行の下に開く欄（操作を選ぶと、その操作の欄だけが使える）。
// 開くのは同時に1つ。表を描き直すと閉じる
let trialForm = null;

const codeInput = (value = "") => el("input", { list: "code-list-2", placeholder: "品番", value, size: 10 });
const qtyInput = (value) => el("input", { value, inputmode: "decimal", size: 4 });

// 操作: { label, text: 文の部品, inputs: 文の中の欄, change: () => API へ送る変更 }
const replaceOp = (part, qty) => {
  const code = codeInput();
  const q = qtyInput(qty);
  return { label: "替える", text: [`${part} を `, code, " に替える　数量 ", q], inputs: [code, q],
    change: () => ({ kind: "REPLACE", part, replacement: code.value.trim(), quantity: q.value }) };
};
const removeOp = (part, qty) => ({ label: "抜く", text: [`${part} を構成から抜く`], inputs: [],
  change: () => ({ kind: "REMOVE", part, quantity: qty }) });
const quantityOp = (part, qty) => {
  const q = qtyInput(qty);
  return { label: "数量", text: [`${part} の数量を `, q, " にする"], inputs: [q],
    change: () => ({ kind: "SET_QUANTITY", part, quantity: q.value }) };
};
// 足すは、どの行から足しても製品（試算の根）の直下へ入る（scenario.py の _apply）
const addOp = (product, code = "") => {
  const c = codeInput(code);
  const q = qtyInput("1");
  return { label: "足す", text: [`${product} の直下に `, c, " を ", q, " 個足す"], inputs: [c, q],
    change: () => ({ kind: "ADD", part: c.value.trim(), quantity: q.value }) };
};

function closeChangeForm() {
  trialForm?.remove();
  trialForm = null;
}

function changeButton(product, makeOps) {
  const button = el("button", { type: "button" }, "変更");
  button.addEventListener("click", () => openChangeForm(button.closest("tr"), product, makeOps()));
  return button;
}

function openChangeForm(row, product, ops) {
  const same = trialForm?.previousElementSibling === row;
  closeChangeForm();
  if (same) return;
  const lines = ops.map((op, i) => ({ op, radio: el("input", { type: "radio", name: "trial-change-kind", checked: i === 0 }) }));
  const sync = () => lines.forEach(({ op, radio }) => op.inputs.forEach((x) => { x.disabled = !radio.checked; }));
  lines.forEach(({ radio }) => radio.addEventListener("change", sync));
  sync();
  const run = () => guarded(() => trialChange(product, lines.find((l) => l.radio.checked).op.change()));
  const colspan = [...row.children].reduce((n, td) => n + td.colSpan, 0);
  trialForm = el("tr", { class: "change-form" },
    el("td", { colspan },
      lines.map(({ op, radio }) => el("div", { class: "change-line" }, el("label", {}, radio, " ", op.label), el("span", {}, op.text))),
      el("div", { class: "change-buttons" },
        el("button", { type: "button", class: "primary", onclick: run }, "実行"),
        el("button", { type: "button", onclick: closeChangeForm }, "やめる"))));
  row.after(trialForm);
}

function trialNodeRow(ctx, node, path, depth, inRoot) {
  const usage = node.usage && !String(node.usage).startsWith("-") && !/^0*(\.0*)?$/.test(node.usage) ? node.usage : "1";
  // 数量は試算の根の直下の行だけ（「製品から」と同じ）
  const ops = () => [
    replaceOp(node.code, usage),
    removeOp(node.code, usage),
    inRoot && ctx.scenarioId ? quantityOp(node.code, usage) : null,
    addOp(ctx.product, node.code),
  ].filter(Boolean);
  return el(
    "tr",
    { class: "group-node" },
    el("td", { style: `padding-left:${8 + depth * 18}px` },
      twistyCell(path, node.has_children), node.code,
      node.quantity === null ? null : el("span", { class: "muted" }, ` ×${fmt(node.quantity)} `), roleTag(node.role)),
    el("td", {}, node.name, " ", nodeMarks(node, TRIAL_HEAD)),
    el("td", { colspan: 4, class: `num ${node.cost === null ? "missing" : ""}` },
      `原価 ${fmt(node.cost)}・金額 ${fmt(node.amount)}`),
    el("td", {}, changeButton(ctx.product, ops)),
  );
}

async function addTrialChildren(rows, ctx, data, path, depth) {
  for (const [index, child] of data.children.entries()) {
    const childPath = `${path}/${child.balloon ?? ""}:${child.code}#${index}`;
    rows.push(trialNodeRow(ctx, child, childPath, depth, path === ctx.product));
    if (child.has_children && trialOpen.has(childPath)) {
      await addTrialChildren(rows, ctx, await trialTree(ctx.t, ctx.product, child.code), childPath, depth + 1);
    }
  }
}

async function trialTreeRows(t, product) {
  const root = await trialTree(t, product);
  if (root.scenario_id !== null && trialScenario.get(product) === null) moveUnderBase(product);
  trialScenario.set(product, root.scenario_id);
  const rows = [];
  await addTrialChildren(rows, { t, product, scenarioId: root.scenario_id }, root, product, 1);
  return rows;
}

// 変えた所だけの表: 製品・変更・その変更での原価の前 → 後と差、最後に合計の差
function changeListBlock(c) {
  const rows = c.rows.map((r) => el(
    "tr",
    {},
    el("td", {}, `${r.product} ${r.product_name}`),
    el("td", {}, el("span", { class: r.kind === "REMOVE" ? "tag remove" : "tag add" }, r.kind_label)),
    el("td", {}, r.text),
    el("td", { class: "num" }, `${fmt(r.cost_before)} → ${fmt(r.cost_after)}`),
    el("td", { class: `num ${signClass(r.difference)}` }, fmt(r.difference, { sign: true, missing: "—" })),
    el("td", { class: "muted" }, `${r.version_id}・${r.head_label}`),
  ));
  const total = el("tr", {},
    el("th", { colspan: 4 }, `合計${c.missing_count ? `（未登録 ${c.missing_count} 件を除く）` : ""}`),
    el("th", { class: `num ${signClass(c.difference)}` }, fmt(c.difference, { sign: true })),
    el("th", {}, ""));
  return [
    el("h3", {}, `変更の一覧（${c.count} 件）`),
    table(["製品", "種類", "変更", numTh("原価 前 → 後"), numTh("差"), "版・費目"],
      rows.length ? [...rows, total] : [el("tr", {}, el("td", { colspan: 6, class: "muted" }, "なし"))]),
  ];
}

function partBlock(parts) {
  const partRows = parts.rows.map((r) => el(
    "tr",
    {},
    el("td", {}, `${r.product} ${r.product_name}`),
    el("td", {}, r.part),
    el("td", {}, r.part_name),
    numTd(r.usage),
    el("td", {}, r.replaced_by.length ? r.replaced_by.map((c) => el("span", { class: "tag add" }, `→ ${c}`)) : "—"),
    el("td", {}, changeButton(r.product, () => [replaceOp(r.part, r.usage)])),
  ));
  return [
    el("h3", {}, `部品「${parts.part}」（${parts.rows.length} 件${parts.truncated ? "（上限）" : ""}）`),
    table(["製品", "部品", "名称", numTh("使用数（個/台）"), "替えた先", ""],
      partRows.length ? partRows : [el("tr", {}, el("td", { colspan: 6, class: "muted" }, "なし"))]),
  ];
}

async function drawTrial() {
  const t = trialOpened;
  if (!t) throw new Error("試作キーを開く");
  const part = document.getElementById("trial-part").value.trim();
  const base = { version: t.version, name: t.name, trial: "1" };
  const [p, parts, changes] = await Promise.all([
    api("GET", `/api/group/products?${qs({ ...base, head: TRIAL_HEAD })}`),
    part ? api("GET", `/api/group/parts?${qs({ ...base, part })}`) : null,
    api("GET", `/api/group/changes?${qs({ version: t.version, name: t.name })}`),
  ]);
  trialForm = null;  // 表を描き直すと開いていた欄は消える
  const productRow = (x) => el(
    "tr",
    {},
    el("td", {}, twistyCell(x.code, x.has_structure || Boolean(x.scenario_id)), x.code),
    el("td", {}, x.name),
    numTd(x.current),
    numTd(x.after),
    el("td", { class: `num ${signClass(x.difference)}` }, fmt(x.difference, { sign: true, missing: "—" })),
    el("td", { class: "num" }, String(x.change_count)),
    el("td", {},
      changeButton(x.code, () => [addOp(x.code)]),
      x.change_count ? el("button", { type: "button", onclick: () => guarded(() => trialUndo(x.scenario_id)) }, "取り消す") : null),
  );
  const productRows = [];
  for (const x of p.products) {
    productRows.push(productRow(x));
    if (trialOpen.has(x.code)) productRows.push(...await trialTreeRows(t, x.code));
  }
  const heads = ["品番", "名称", numTh(`今（${p.head_label}）`), numTh("後"), numTh("差"), numTh("変更"), ""];
  document.getElementById("trial-body").replaceChildren(
    el("p", { class: "premise-line" }, `前提: 版 ${p.version_id}・試作キー ${p.name}・${p.head_label}・参考値`),
    el("h3", {}, `製品（${countText(p.count, p.products.length, p.truncated)}）`),
    table(heads, productRows.length ? productRows : [el("tr", {}, el("td", { colspan: heads.length, class: "muted" }, "なし"))]),
    ...(parts ? partBlock(parts) : []),
    ...changeListBlock(changes),
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
