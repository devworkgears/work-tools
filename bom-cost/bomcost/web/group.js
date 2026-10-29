// 製品群: 版の製品を並べ、製品の行の下に構成を開いて替える・抜く・足す、部品の名前で絞った製品×部品の行を替える。app.js・screens.js の関数を使う。
// 製品ごとの試算（画面では試作品）は API が name で引く（「試作品を作る」でまとめて作るか、無ければ最初の変更で作る）。原価は API の文字列をそのまま出す。
"use strict";

function groupInputs() {
  return {
    version: document.getElementById("group-version").value,
    head: document.getElementById("group-head").value,
    name: document.getElementById("group-name").value.trim(),
    q: document.getElementById("group-q").value.trim(),
    part: document.getElementById("group-part").value.trim(),
  };
}

async function enterGroup() {
  const version = document.getElementById("group-version");
  versionOptions(version, { value: version.value || state.summary?.scenario.version_id });
  options(document.getElementById("group-head"), state.meta.heads.map((h) => [h.id, h.label]));
  await loadGroupNames();
  await loadCodes(version.value);
  groupOpen.clear();  // 開いたときは製品の行を全部閉じて並べる
  groupScenario.clear();
  if (version.value) await drawGroup();
}

async function loadGroupNames() {
  const version = document.getElementById("group-version").value;
  if (!version) return;
  const names = await api("GET", `/api/group/names?${qs({ version })}`);
  document.getElementById("group-names").replaceChildren(...names.map((n) => el("option", { value: n })));
}

async function groupChange(product, change) {
  const g = groupInputs();
  if (!g.name) throw new Error("試作品の名前が空");
  await api("POST", "/api/group/changes", { version_id: g.version, name: g.name, head: g.head, product, ...change });
  await loadLists();
  await loadGroupNames();
  await drawGroup();
}

// 一覧に出ている製品（絞り込み後）のすべてに、その製品を土台にした試作品（試算）を作る。既にあれば作らない
async function createTrials() {
  const g = groupInputs();
  if (!g.name) throw new Error("試作品の名前が空");
  const r = await api("POST", "/api/group/scenarios", { version_id: g.version, name: g.name, head: g.head, q: g.q });
  await loadLists();
  await loadGroupNames();
  await drawGroup();
  showMessage(`試作品 作成 ${r.created} 件・既存 ${r.existing} 件${r.truncated ? "（上限）" : ""}`, "info");
}

async function groupUndo(scenarioId) {
  await api("POST", `/api/scenarios/${scenarioId}/undo`, {});
  await drawGroup();
}

// 製品の行の下に開く構成。行の形は「製品から」のツリーと同じ（API が検討名の試算か版の構成を返す）。
// 開いた所は道筋（製品/風船:品番#並び順/…）で持ち、表を取り直しても開いたままにする
const groupOpen = new Set();
const groupScenario = new Map(); // 製品 → 前に開いた時の試算の ID（無ければ null）

function toggleGroup(path) {
  if (groupOpen.has(path)) groupOpen.delete(path);
  else {
    groupOpen.add(path);
    // 製品の行を開いたら、試作品の「土台」の行も開いて部品まで見せる（版の構成なら当たる行が無く効かない）
    if (!path.includes("/")) groupOpen.add(`${path}/:${path}#0`);
  }
  guarded(drawGroup);
}

function groupTree(g, product, code) {
  return api("GET", `/api/group/tree?${qs({ version: g.version, head: g.head, name: g.name, product, code })}`);
}

// 試算ができると、版の構成は試算の根の直下の「土台」の行（並び順 0）の下へ1段下がる。開いていた所もそこへ移す
function moveUnderBase(product) {
  const prefix = `${product}/`;
  const base = `${product}/:${product}#0`;
  for (const path of [...groupOpen]) {
    if (!path.startsWith(prefix)) continue;
    groupOpen.delete(path);
    groupOpen.add(`${base}/${path.slice(prefix.length)}`);
  }
  groupOpen.add(base);
}

function twistyCell(path, canOpen) {
  return el("span", { class: "group-twisty", onclick: () => canOpen && toggleGroup(path) },
    canOpen ? (groupOpen.has(path) ? "▼" : "▶") : "");
}

function groupNodeRow(ctx, node, path, depth, inRoot) {
  const usage = node.usage && !String(node.usage).startsWith("-") && !/^0*(\.0*)?$/.test(node.usage) ? node.usage : "1";
  const qty = el("input", { value: usage, inputmode: "decimal", size: 4 });
  const repl = el("input", { list: "code-list-2", placeholder: "代わりの品番", size: 10 });
  const addCode = el("input", { list: "code-list-2", value: node.code, size: 10 });
  const send = (kind, extra = {}) =>
    guarded(() => groupChange(ctx.product, { kind, part: node.code, quantity: qty.value, ...extra }));
  const cost = el("span", { class: `num ${node.cost === null ? "missing" : ""}` },
    `原価 ${fmt(node.cost)}・金額 ${fmt(node.amount)}`);
  return el(
    "tr",
    { class: "group-node" },
    el("td", { style: `padding-left:${8 + depth * 18}px` },
      twistyCell(path, node.has_children), node.code,
      node.quantity === null ? null : el("span", { class: "muted" }, ` ×${fmt(node.quantity)} `), roleTag(node.role)),
    el("td", {}, node.name, " ", nodeMarks(node, ctx.g.head), ctx.withCost ? null : [" ", cost]),
    ctx.withCost ? el("td", { colspan: 4, class: "num" }, cost) : null,
    el("td", {},
      "数量 ", qty,
      el("button", { type: "button", onclick: () => send("REPLACE", { replacement: repl.value.trim() }) }, "替える"), repl,
      el("button", { type: "button", onclick: () => send("REMOVE") }, "抜く"),
      el("button", { type: "button", onclick: () => send("ADD", { part: addCode.value.trim() }) }, "足す"), addCode,
      // 数量にするは試算の根の直下の行だけ（「製品から」と同じ）
      inRoot && ctx.scenarioId ? el("button", { type: "button", onclick: () => send("SET_QUANTITY") }, "数量にする") : null),
  );
}

async function addGroupChildren(rows, ctx, data, path, depth) {
  for (const [index, child] of data.children.entries()) {
    const childPath = `${path}/${child.balloon ?? ""}:${child.code}#${index}`;
    rows.push(groupNodeRow(ctx, child, childPath, depth, path === ctx.product));
    if (child.has_children && groupOpen.has(childPath)) {
      await addGroupChildren(rows, ctx, await groupTree(ctx.g, ctx.product, child.code), childPath, depth + 1);
    }
  }
}

async function groupTreeRows(g, product, withCost) {
  const root = await groupTree(g, product);
  if (root.scenario_id !== null && groupScenario.get(product) === null) moveUnderBase(product);
  groupScenario.set(product, root.scenario_id);
  const rows = [];
  await addGroupChildren(rows, { g, product, withCost, scenarioId: root.scenario_id }, root, product, 1);
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

// 部品（試作の部品など）をその場で登録する（手入力の版に入る）。登録した品番はすぐ替える・足すの候補に出る
function trialItemBlock() {
  const code = el("input", { size: 10 });
  const name = el("input", { size: 12 });
  const costs = Object.fromEntries(state.meta.heads.map((h) => [h.id, el("input", { inputmode: "decimal", size: 7 })]));
  const save = async () => {
    await api("PUT", "/api/manual/items", {
      code: code.value.trim(), name: name.value.trim(),
      costs: Object.fromEntries(Object.entries(costs).map(([k, input]) => [k, input.value])),
    });
    await loadCodes(groupInputs().version);
    showMessage(`部品 ${code.value.trim()} を登録`, "info");
  };
  return el("details", {},
    el("summary", {}, "部品を登録"),
    el("div", { class: "form-row" },
      el("label", {}, "品番 ", code), el("label", {}, "名称 ", name),
      state.meta.heads.map((h) => el("label", {}, `${h.label} `, costs[h.id])),
      el("button", { type: "button", class: "primary", onclick: () => guarded(save) }, "登録")));
}

function countText(n, shown, truncated) {
  return truncated ? `${shown} / ${n} 件（上限）` : `${n} 件`;
}

async function drawGroup() {
  const g = groupInputs();
  if (!g.version) throw new Error("版が空");
  const [p, parts, changes] = await Promise.all([
    api("GET", `/api/group/products?${qs({ version: g.version, head: g.head, name: g.name, q: g.q })}`),
    g.part ? api("GET", `/api/group/parts?${qs({ version: g.version, name: g.name, q: g.q, part: g.part })}`) : null,
    g.name ? api("GET", `/api/group/changes?${qs({ version: g.version, name: g.name })}`) : null,
  ]);
  const withCost = Boolean(p.name);
  const productRow = (x) => {
    const addPart = el("input", { list: "code-list-2", placeholder: "品番", size: 10 });
    const addQty = el("input", { value: "1", inputmode: "decimal", size: 4 });
    return el(
      "tr",
      {},
      el("td", {}, twistyCell(x.code, x.has_structure || Boolean(x.scenario_id)), x.code),
      el("td", {}, x.name),
      withCost ? [
        numTd(x.current),
        numTd(x.after),
        el("td", { class: `num ${signClass(x.difference)}` }, fmt(x.difference, { sign: true, missing: "—" })),
        el("td", { class: "num" }, String(x.change_count)),
      ] : null,
      el("td", {},
        addPart, addQty,
        el("button", { type: "button", onclick: () => guarded(() => groupChange(x.code, { kind: "ADD", part: addPart.value.trim(), quantity: addQty.value })) }, "足す"),
        withCost && x.change_count ? el("button", { type: "button", onclick: () => guarded(() => groupUndo(x.scenario_id)) }, "取り消す") : null),
    );
  };
  const productRows = [];
  for (const x of p.products) {
    productRows.push(productRow(x));
    if (groupOpen.has(x.code)) productRows.push(...await groupTreeRows(g, x.code, withCost));
  }
  const productHeads = ["品番", "名称", ...(withCost ? [numTh(`今（${p.head_label}）`), numTh("後"), numTh("差"), numTh("変更")] : []), ""];
  const blocks = [trialItemBlock(), ...(changes ? changeListBlock(changes) : [])];
  blocks.push(
    el("h3", {}, `製品（${countText(p.count, p.products.length, p.truncated)}・区分 ${p.kind ?? "—"}）`),
    table(productHeads, productRows.length ? productRows : [el("tr", {}, el("td", { colspan: productHeads.length, class: "muted" }, "なし"))]),
  );
  if (parts) {
    const partRows = parts.rows.map((r) => {
      const repl = el("input", { list: "code-list-2", placeholder: "代わりの品番", size: 10 });
      const qty = el("input", { value: r.usage, inputmode: "decimal", size: 4 });
      return el(
        "tr",
        {},
        el("td", {}, `${r.product} ${r.product_name}`),
        el("td", {}, r.part),
        el("td", {}, r.part_name),
        numTd(r.usage),
        el("td", {}, r.replaced_by.length ? r.replaced_by.map((c) => el("span", { class: "tag add" }, `→ ${c}`)) : "—"),
        el("td", {},
          repl, qty,
          el("button", { type: "button", onclick: () => guarded(() => groupChange(r.product, { kind: "REPLACE", part: r.part, replacement: repl.value.trim(), quantity: qty.value })) }, "替える")),
      );
    });
    blocks.push(
      el("h3", {}, `部品「${parts.part}」（${parts.truncated ? `${parts.rows.length} 件（上限）` : `${parts.rows.length} 件`}）`),
      table(["製品", "部品", "名称", numTh("使用数（個/台）"), "替えた先", ""],
        partRows.length ? partRows : [el("tr", {}, el("td", { colspan: 6, class: "muted" }, "なし"))]),
    );
  }
  document.getElementById("group-body").replaceChildren(
    el("p", { class: "premise-line" }, `前提: 版 ${p.version_id}・費目 ${p.head_label}・試作品 ${p.name || "—"}・参考値・償却費除外`),
    ...blocks,
  );
}

function wireGroup() {
  document.getElementById("group-show").addEventListener("click", () => guarded(drawGroup));
  document.getElementById("group-create").addEventListener("click", () => guarded(createTrials));
  for (const id of ["group-name", "group-q", "group-part"]) {
    document.getElementById(id).addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawGroup));
  }
  document.getElementById("group-version").addEventListener("change", (e) => guarded(async () => {
    await loadGroupNames();
    await loadCodes(e.target.value);
  }));
}

wireGroup();
