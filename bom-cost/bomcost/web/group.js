// 製品群: 版の製品を並べ、部品の名前で絞った製品×部品の行を替える・製品ごとに足す。app.js・screens.js の関数を使う。
// 製品ごとの試算は API が検討名で引く（無ければ最初の変更で作る）。原価は API の文字列をそのまま出す。
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
}

async function loadGroupNames() {
  const version = document.getElementById("group-version").value;
  if (!version) return;
  const names = await api("GET", `/api/group/names?${qs({ version })}`);
  document.getElementById("group-names").replaceChildren(...names.map((n) => el("option", { value: n })));
}

async function groupChange(product, change) {
  const g = groupInputs();
  if (!g.name) throw new Error("検討名が空");
  await api("POST", "/api/group/changes", { version_id: g.version, name: g.name, head: g.head, product, ...change });
  await loadLists();
  await loadGroupNames();
  await drawGroup();
}

async function groupUndo(scenarioId) {
  await api("POST", `/api/scenarios/${scenarioId}/undo`, {});
  await drawGroup();
}

async function openGroupScenario(scenarioId) {
  await loadLists();
  await selectScenario(scenarioId);
  await showScreen("product");
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

// 試作品をその場で登録する（手入力の版に入る）。登録した品番はすぐ替える・足すの候補に出る
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
    showMessage(`試作品 ${code.value.trim()} を登録`, "info");
  };
  return el("details", {},
    el("summary", {}, "試作品を登録"),
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
  const productRows = p.products.map((x) => {
    const addPart = el("input", { list: "code-list-2", placeholder: "品番", size: 10 });
    const addQty = el("input", { value: "1", inputmode: "decimal", size: 4 });
    return el(
      "tr",
      {},
      el("td", {}, x.code),
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
        withCost && x.change_count ? el("button", { type: "button", onclick: () => guarded(() => groupUndo(x.scenario_id)) }, "取り消す") : null,
        withCost && x.scenario_id ? el("button", { type: "button", onclick: () => guarded(() => openGroupScenario(x.scenario_id)) }, "開く") : null),
    );
  });
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
    el("p", { class: "premise-line" }, `前提: 版 ${p.version_id}・費目 ${p.head_label}・検討名 ${p.name || "—"}・参考値・償却費除外`),
    ...blocks,
  );
}

function wireGroup() {
  document.getElementById("group-show").addEventListener("click", () => guarded(drawGroup));
  for (const id of ["group-name", "group-q", "group-part"]) {
    document.getElementById(id).addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawGroup));
  }
  document.getElementById("group-version").addEventListener("change", (e) => guarded(async () => {
    await loadGroupNames();
    await loadCodes(e.target.value);
  }));
}

wireGroup();
