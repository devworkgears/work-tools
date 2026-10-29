// 画面の2段目: 部品から見る・モデルチェンジ・手入力・取り込みと版。app.js の el・fmt・money・api・guarded を使う。
// 原価は API が出した Decimal の文字列をそのまま出し、ここでは計算しない。
"use strict";

const MAX_FILE_BYTES = 16 * 1024 * 1024; // サーバーの上限と同じ
const screenState = {
  screen: "group",
  allocation: "BY_USAGE",
  allocations: [],
  manual: null,
  editing: {}, // 手入力の表ごとに「直す元」の行
  preview: null, // { file, content, data }
  pending: [], // 取り込む一覧
};

// 共通 -------------------------------------------------------------------

function options(select, list, { value, blank } = {}) {
  const keep = value ?? select.value;
  select.replaceChildren(
    ...(blank ? [el("option", { value: "" }, blank)] : []),
    ...list.map(([v, label]) => el("option", { value: v }, label)),
  );
  if (keep && [...select.options].some((o) => o.value === String(keep))) select.value = String(keep);
}

function versionOptions(select, opts = {}) {
  options(select, state.versions.map((v) => [v.version_id, v.version_id]), opts);
}

function scenarioOptions(select, opts = {}) {
  options(select, state.scenarios.map((s) => [s.scenario_id, `${s.name}（${s.base} 土台・版 ${s.version_id}）`]), opts);
}

async function loadCodes(versionId) {
  if (!versionId) return;
  const items = await api("GET", `/api/items?version=${encodeURIComponent(versionId)}`);
  document.getElementById("code-list-2").replaceChildren(...items.map((i) => el("option", { value: i.code }, i.name)));
}

async function refreshLists() {
  await loadLists();
  if (state.summary) {
    document.getElementById("scenario-select").value = String(state.summary.scenario.scenario_id);
    document.getElementById("version-select").value = state.summary.scenario.version_id;
  }
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") p.set(k, v);
  return p.toString();
}

function table(headers, rows, cls = "small") {
  return el(
    "table",
    { class: cls },
    el("thead", {}, el("tr", {}, headers.map((h) => (typeof h === "string" ? el("th", {}, h) : h)))),
    el("tbody", {}, rows),
  );
}

function numTh(text) {
  return el("th", { class: "num" }, text);
}

function numTd(text, opts) {
  return el("td", { class: `num ${text === null || text === undefined ? "missing" : ""}` }, fmt(text, opts));
}

// 画面の切り替え -----------------------------------------------------------

const SCREENS = { product: "main-layout", group: "screen-group", compare: "screen-compare", part: "screen-part", change: "screen-change", manual: "screen-manual", import: "screen-import" };

async function showScreen(name) {
  screenState.screen = name;
  setSeg("screen-seg", "screen", name);
  for (const [key, id] of Object.entries(SCREENS)) document.getElementById(id).hidden = key !== name;
  document.getElementById("product-controls").hidden = name !== "product";
  showMessage("");
  if (name === "product") {
    if (!state.scenarioId && state.scenarios.length) await selectScenario(state.scenarios[0].scenario_id);
  } else if (name === "group") await enterGroup();
  else if (name === "compare") await enterCompare();
  else if (name === "part") await enterPart();
  else if (name === "change") await enterChange();
  else if (name === "manual") await renderManual();
  else if (name === "import") await enterImport();
}

// 部品から見る --------------------------------------------------------------

async function enterPart() {
  const version = document.getElementById("part-version");
  const scenario = document.getElementById("part-scenario");
  versionOptions(version, { value: version.value || state.summary?.scenario.version_id });
  scenarioOptions(scenario, { blank: "使わない（版の構成だけ）", value: scenario.value || state.scenarioId || "" });
  options(document.getElementById("part-head"), state.meta.heads.map((h) => [h.id, h.label]));
  if (!screenState.allocations.length) {
    const manual = await api("GET", "/api/manual");
    screenState.allocations = manual.allocations;
  }
  const seg = document.getElementById("part-allocation");
  seg.replaceChildren(
    ...screenState.allocations.map((a) =>
      el("button", {
        type: "button", "data-allocation": a.id, class: a.id === screenState.allocation ? "on" : "",
        onclick: () => guarded(async () => {
          screenState.allocation = a.id;
          setSeg("part-allocation", "allocation", a.id);
          if (document.getElementById("part-code").value) await drawPart();
        }),
      }, a.label),
    ),
  );
  await loadCodes(version.value);
}

async function drawPart() {
  const scenarioId = document.getElementById("part-scenario").value;
  const v = await api("GET", `/api/parts/view?${qs({
    part: document.getElementById("part-code").value.trim(),
    version: document.getElementById("part-version").value,
    scenario: scenarioId,
    head: document.getElementById("part-head").value,
    allocation: screenState.allocation,
  })}`);
  const p = v.premises;
  const mold = v.mold ? `型費 ${fmt(v.mold.amount)} 円・${fmt(v.mold.years)}年` : "型費 未登録";
  const title = el("div", { class: "detail-title" }, `部品から見る: ${v.part}（${[v.name, mold].filter(Boolean).join("・")}）`);
  const headLabel = p.head_label;
  const rows = v.rows.map((r) =>
    el(
      "tr",
      {},
      el("td", {}, `${r.product} ${r.name}`),
      el("td", {}, `${r.volume_kind}（${r.via}）`),
      numTd(r.annual),
      numTd(r.quantity_per_unit),
      numTd(r.annual_count),
      numTd(r.mold_per_unit, { missing: "—" }),
      el("td", { class: "num" }, r.part_difference === null ? "—" : `${fmt(r.part_difference, { sign: true })}（${r.replaced_part || "古い部品なし"} → ${v.part}）`),
      el("td", { class: "num" }, `${fmt(r.current, { missing: "—" })} → ${fmt(r.after, { missing: "—" })}`),
      el("td", { class: `num ${signClass(r.difference)}` }, fmt(r.difference, { sign: true, missing: "—" })),
      el("td", { class: `num ${signClass(r.difference_with_amortization)}` }, fmt(r.difference_with_amortization, { sign: true, missing: "—" })),
    ),
  );
  const main = table(
    ["使う製品", "種別", numTh("年間台数"), numTh("使用数（個/台）"), numTh("年間の数（個）"), numTh("償却費＝型費（円/台）"),
      numTh(`部品の差（${headLabel}・円/台）`), numTh(`今 → 後（${headLabel}・円/台）`), numTh(`差（${headLabel}）`), numTh("差（償却費込み）")],
    rows.length ? rows : [el("tr", {}, el("td", { colspan: 10, class: "muted" },"なし"))],
  );
  const summary = el(
    "div",
    { class: "result-line" },
    el("span", {}, el("span", { class: "label" }, "部品の年間の数"), el("span", { class: "big num" }, fmt(v.annual_count)), " 個"),
    el("span", {}, el("span", { class: "label" }, "1個あたり型費"), el("span", { class: "big num" }, fmt(v.per_part, { missing: "—" })), " 円"),
    el("span", {}, el("span", { class: "label" }, "載せ方"), v.allocation_label),
  );
  const breakdown = v.rows
    .filter((r) => r.part_difference !== null && r.mold_per_unit !== null && r.difference_with_amortization !== null)
    .map((r) => el("p", { class: "muted" }, `${r.product} の差の内訳: 部品の置き換え（${headLabel}） ${fmt(r.part_difference, { sign: true })} ＋ 償却費（型費） ${fmt(r.mold_per_unit, { sign: true })} ＝ ${fmt(r.difference_with_amortization, { sign: true })}`));
  const premise = el(
    "p",
    { class: "premise-line" },
    `前提: 版 ${p.version_id}・費目 ${p.head_label}・台数 ${p.volumes.map((x) => `${x.product} ${x.kind}`).join(" / ") || "—"}・載せ方 ${p.allocation_label}`
      + `・構成 ${p.scenario_name ? `試算「${p.scenario_name}」` : "版"}・${p.reference}・償却費除外`,
  );
  const unknown = v.unknown_volume.length
    ? el("p", { class: "missing" }, `年間台数なし: ${v.unknown_volume.join("・")}`)
    : null;
  const heads = v.heads;
  const allHeads = table(
    ["使う製品", ...heads.map((h) => numTh(`${h.label} 今 → 後`))],
    [
      el("tr", {}, el("td", {}, `部品 ${v.part} の原価`), heads.map((h) => numTd(v.part_costs[h.id]))),
      ...v.rows.map((r) =>
        el("tr", {}, el("td", {}, r.product), heads.map((h) => {
          const x = r.heads[h.id];
          return el("td", { class: "num" }, `${fmt(x.current, { missing: "—" })} → ${fmt(x.after, { missing: "—" })}`);
        })),
      ),
    ],
  );
  document.getElementById("part-body").replaceChildren(
    title, main, summary, ...breakdown, premise, ...(unknown ? [unknown] : []),
    el("h3", {}, "費目別（今 → 後・円/台）"), allHeads,
  );
}

// モデルチェンジ ------------------------------------------------------------

async function enterChange() {
  const oldV = document.getElementById("change-old-version");
  const newV = document.getElementById("change-new-version");
  const current = state.summary?.scenario.version_id;
  versionOptions(oldV, { value: oldV.value || current });
  versionOptions(newV, { value: newV.value || current });
  scenarioOptions(document.getElementById("change-scenario"), { value: document.getElementById("change-scenario").value || state.scenarioId });
  await loadCodes(oldV.value);
}

function changeKindToggle() {
  const byScenario = document.getElementById("change-new-kind").value === "scenario";
  for (const n of document.querySelectorAll(".change-new-product")) n.hidden = byScenario;
  for (const n of document.querySelectorAll(".change-new-scenario")) n.hidden = !byScenario;
}

async function drawChange() {
  const byScenario = document.getElementById("change-new-kind").value === "scenario";
  const params = {
    old: document.getElementById("change-old").value.trim(),
    old_version: document.getElementById("change-old-version").value,
    direct_only: document.getElementById("change-direct").checked ? "1" : "",
  };
  if (byScenario) params.scenario = document.getElementById("change-scenario").value;
  else Object.assign(params, { new: document.getElementById("change-new").value.trim(), new_version: document.getElementById("change-new-version").value });
  const c = await api("GET", `/api/modelchange?${qs(params)}`);
  if (!params.old) document.getElementById("change-old").value = c.old_product;
  const section = (title, kinds) => {
    const rows = c.rows.filter((r) => kinds.includes(r.kind));
    return [
      el("h3", {}, `${title}（${rows.length} 件）`),
      table(
        ["区分", "元の部品", "新しい部品", numTh("元の数量"), numTh("新しい数量"), numTh("差")],
        rows.length
          ? rows.map((r) => el(
            "tr",
            {},
            el("td", {}, el("span", { class: r.kind === "ADDED" ? "tag add" : r.kind === "REMOVED" ? "tag remove" : "tag" }, r.kind_label)),
            el("td", {}, r.old_part ? `${r.old_part} ${r.old_name}` : "—"),
            el("td", {}, r.new_part ? `${r.new_part} ${r.new_name}` : "—"),
            numTd(r.old_quantity), numTd(r.new_quantity),
            el("td", { class: `num ${signClass(r.difference)}` }, fmt(r.difference, { sign: true })),
          ))
          : [el("tr", {}, el("td", { colspan: 6, class: "muted" }, "なし"))],
      ),
    ];
  };
  const newLabel = c.scenario_name ? `試算「${c.scenario_name}」の ${c.new_product}（版 ${c.new_version}・土台＋変更）` : `${c.new_product}（版 ${c.new_version}）`;
  const oldPart = el("input", { list: "code-list-2", placeholder: "元の部品" });
  const newPart = el("input", { list: "code-list-2", placeholder: "新しい部品" });
  const oldBalloon = el("input", { placeholder: "元の風船番号（任意）", size: 10 });
  const newBalloon = el("input", { placeholder: "新しい風船番号（任意）", size: 10 });
  const addMap = el("button", {
    type: "button",
    onclick: () => guarded(async () => {
      await api("POST", "/api/partmaps", {
        old_product: c.old_product, new_product: c.new_product, old_part: oldPart.value, new_part: newPart.value,
        old_balloon: oldBalloon.value, new_balloon: newBalloon.value,
      });
      await drawChange();
      showMessage(`対応表に ${oldPart.value} → ${newPart.value} を足しました。`, "info");
    }),
  }, "対応表に足す");
  const maps = c.part_maps.length
    ? table(["元の部品", "新しい部品", ""], c.part_maps.map((m) => el(
      "tr",
      {},
      el("td", {}, `${m.old_part}${m.old_balloon ? `（${m.old_balloon}）` : ""}`),
      el("td", {}, `${m.new_part}${m.new_balloon ? `（${m.new_balloon}）` : ""}`),
      el("td", {}, el("button", { type: "button", onclick: () => guarded(async () => {
        await api("DELETE", `/api/partmaps?${qs(m)}`);
        await drawChange();
      }) }, "消す")),
    )))
    : el("p", { class: "muted" }, "なし");
  document.getElementById("change-body").replaceChildren(
    el("div", { class: "detail-title" }, `${c.old_product}（版 ${c.old_version}）→ ${newLabel}`),
    el("p", { class: "muted" }, c.direct_only ? "比べ方: 直下の子" : "比べ方: 末端の部品・1台あたりの使用数"),
    ...section("1. 同じ品番", ["SAME"]),
    ...section("2. 対応表で対応", ["MAPPED"]),
    ...section("3. 片方だけ（足した・抜いた）", ["ADDED", "REMOVED"]),
    el("h3", {}, `部品の対応表（${c.old_product} → ${c.new_product}）`),
    maps,
    el("div", { class: "form-row" }, oldPart, oldBalloon, el("span", {}, "→"), newPart, newBalloon, addMap),
  );
}

// 手入力 --------------------------------------------------------------------

function field(label, input) {
  return el("label", {}, label, input);
}

function card(title, body) {
  return el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("h2", {}, title)), el("div", { class: "screen-body" }, body));
}

async function renderManual(data) {
  const m = data || (await api("GET", "/api/manual"));
  screenState.manual = m;
  screenState.allocations = m.allocations;
  const send = (method, path, body) => guarded(async () => {
    const result = await api(method, path, body);
    screenState.editing = {};
    await renderManual(Array.isArray(result) ? null : result);
    showMessage("保存しました。", "info");
  });
  const del = (path) => () => {
    if (confirm("消しますか？")) send("DELETE", path);
  };
  const edit = (key, values) => () => {
    screenState.editing[key] = values;
    renderManual(screenState.manual);
  };
  const val = (key, name, fallback = "") => (screenState.editing[key]?.[name] ?? fallback) ?? "";

  // 品目と原価
  const itemInputs = {
    code: el("input", { value: val("item", "code"), placeholder: "例 X-610" }),
    name: el("input", { value: val("item", "name"), placeholder: "例 試作品" }),
    kind: el("input", { value: val("item", "kind"), size: 4 }),
  };
  const costInputs = Object.fromEntries(m.heads.map((h) => [h.id, el("input", { value: val("item", h.id), inputmode: "decimal", size: 9 })]));
  const items = el("div", {},
    table(["品番", "名称", "区分", ...m.heads.map((h) => numTh(h.label)), ""], m.items.map((i) => el(
      "tr", {},
      el("td", {}, i.code), el("td", {}, i.name), el("td", {}, i.kind),
      m.heads.map((h) => numTd(i.costs[h.id])),
      el("td", {},
        el("button", { type: "button", onclick: edit("item", { code: i.code, name: i.name, kind: i.kind, ...i.costs }) }, "直す"),
        el("button", { type: "button", onclick: del(`/api/manual/items?${qs({ code: i.code })}`) }, "消す")),
    ))),
    el("div", { class: "form-row" },
      field("品番", itemInputs.code), field("名称", itemInputs.name), field("区分", itemInputs.kind),
      m.heads.map((h) => field(h.label, costInputs[h.id])),
      el("button", { type: "button", class: "primary", onclick: () => send("PUT", "/api/manual/items", {
        code: itemInputs.code.value, name: itemInputs.name.value, kind: itemInputs.kind.value,
        costs: Object.fromEntries(m.heads.map((h) => [h.id, costInputs[h.id].value])),
      }) }, "保存")),
  );

  // 年間台数
  const volProduct = el("input", { value: val("volume", "product"), placeholder: "例 V-110" });
  const volAnnual = el("input", { value: val("volume", "annual"), inputmode: "decimal" });
  const volumes = el("div", {},
    table(["製品の品番", numTh("年間台数"), "種別", ""], m.volumes.map((v) => el("tr", {},
      el("td", {}, v.product), numTd(v.annual), el("td", {}, v.kind),
      el("td", {},
        el("button", { type: "button", onclick: edit("volume", v) }, "直す"),
        el("button", { type: "button", onclick: del(`/api/manual/volumes?${qs({ product: v.product })}`) }, "消す")),
    ))),
    el("div", { class: "form-row" }, field("製品の品番", volProduct), field("年間台数", volAnnual),
      el("button", { type: "button", class: "primary", onclick: () => send("PUT", "/api/manual/volumes", { product: volProduct.value, annual: volAnnual.value }) }, "保存")),
  );

  // 型費
  const moldPart = el("input", { value: val("mold", "part"), placeholder: "例 X-610" });
  const moldAmount = el("input", { value: val("mold", "amount"), inputmode: "decimal", placeholder: "円" });
  const moldYears = el("input", { value: val("mold", "years", "2"), inputmode: "decimal", size: 4 });
  const molds = el("div", {},
    table(["部品の品番", numTh("型費（円）"), numTh("償却年数"), numTh("1年あたり（円）"), ""], m.molds.map((x) => el("tr", {},
      el("td", {}, x.part), numTd(x.amount), numTd(x.years), numTd(x.annual_amount),
      el("td", {},
        el("button", { type: "button", onclick: edit("mold", x) }, "直す"),
        el("button", { type: "button", onclick: del(`/api/molds?${qs({ part: x.part })}`) }, "消す")),
    ))),
    el("div", { class: "form-row" }, field("部品の品番", moldPart), field("型費（円）", moldAmount), field("償却年数", moldYears),
      el("button", { type: "button", class: "primary", onclick: () => send("PUT", "/api/molds", { part: moldPart.value, amount: moldAmount.value, years: moldYears.value }) }, "保存")),
  );

  // 流用先
  const reusePart = el("input", { value: val("reuse", "part"), placeholder: "新しい部品" });
  const reuseProduct = el("input", { value: val("reuse", "product"), placeholder: "流用先の製品" });
  const reuseQty = el("input", { value: val("reuse", "quantity", "1"), inputmode: "decimal", size: 4 });
  const reuseOld = el("input", { value: val("reuse", "replaced_part"), placeholder: "置き換わる古い部品（任意）" });
  const reuse = el("div", {},
    table(["部品", "流用先の製品", numTh("使用数（個/台）"), "置き換わる古い部品", ""], m.reuse.map((t) => el("tr", {},
      el("td", {}, t.part), el("td", {}, t.product), numTd(t.quantity), el("td", {}, t.replaced_part || "—"),
      el("td", {},
        el("button", { type: "button", onclick: edit("reuse", { ...t, original: { part: t.part, product: t.product } }) }, "直す"),
        el("button", { type: "button", onclick: del(`/api/reuse?${qs({ part: t.part, product: t.product })}`) }, "消す")),
    ))),
    el("div", { class: "form-row" }, field("部品", reusePart), field("流用先", reuseProduct), field("使用数", reuseQty), field("古い部品", reuseOld),
      el("button", { type: "button", class: "primary", onclick: () => send("PUT", "/api/reuse", {
        part: reusePart.value, product: reuseProduct.value, quantity: reuseQty.value, replaced_part: reuseOld.value,
        original: screenState.editing.reuse?.original,
      }) }, "保存")),
  );

  // 部品の対応表
  const mapInputs = Object.fromEntries(["old_product", "new_product", "old_part", "new_part", "old_balloon", "new_balloon"]
    .map((k) => [k, el("input", { value: val("map", k), size: 10 })]));
  const mapBody = () => Object.fromEntries(Object.entries(mapInputs).map(([k, i]) => [k, i.value]));
  const original = screenState.editing.map?.original;
  const maps = el("div", {},
    table(["元の製品", "新しい製品", "元の部品（風船番号）", "新しい部品（風船番号）", ""], m.part_maps.map((r) => el("tr", {},
      el("td", {}, r.old_product), el("td", {}, r.new_product),
      el("td", {}, `${r.old_part}${r.old_balloon ? `（${r.old_balloon}）` : ""}`),
      el("td", {}, `${r.new_part}${r.new_balloon ? `（${r.new_balloon}）` : ""}`),
      el("td", {},
        el("button", { type: "button", onclick: edit("map", { ...r, original: r }) }, "直す"),
        el("button", { type: "button", onclick: del(`/api/partmaps?${qs(r)}`) }, "消す")),
    ))),
    el("div", { class: "form-row" },
      field("元の製品", mapInputs.old_product), field("新しい製品", mapInputs.new_product),
      field("元の部品", mapInputs.old_part), field("元の風船番号", mapInputs.old_balloon),
      field("新しい部品", mapInputs.new_part), field("新しい風船番号", mapInputs.new_balloon),
      el("button", { type: "button", class: "primary", onclick: () => (original
        ? send("PUT", "/api/partmaps", { ...mapBody(), original })
        : send("POST", "/api/partmaps", mapBody())) }, original ? "直して保存" : "足す")),
  );

  document.getElementById("manual-body").replaceChildren(
    card("試作品の品目と原価", items),
    card("年間台数（新製品の計画）", volumes),
    card("型費", molds),
    card("流用先", reuse),
    card("部品の対応表", maps),
  );
}

// 取り込みと版 --------------------------------------------------------------

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error(`${file.name} を読めない`));
    reader.readAsDataURL(file);
  });
}

async function enterImport() {
  await drawVersions();
}

async function previewFile(sheet) {
  const input = document.getElementById("import-file");
  const file = input.files[0];
  if (!file) return;
  if (file.size > MAX_FILE_BYTES) throw new Error(`${file.name} が大きすぎる（上限 16 MB）`);
  const content = screenState.preview?.file === file ? screenState.preview.content : await readAsBase64(file);
  const data = await api("POST", "/api/import/preview", { filename: file.name, content, sheet });
  screenState.preview = { file, content, data };
  const sheetSelect = document.getElementById("import-sheet");
  document.getElementById("import-sheet-label").hidden = !data.sheets.length;
  options(sheetSelect, data.sheets.map((s) => [s, s]), { value: data.sheet });
  const tableSelect = document.getElementById("import-table");
  options(tableSelect, data.tables.map((t) => [t.id, `${t.label}${t.saved ? "（保存した対応づけあり）" : ""}`]), { value: data.suggested || guessTable(data) || tableSelect.value });
  drawMapping();
}

// 保存した対応づけが無いときは、見出しの名前で当たる項目がいちばん多い表を仮に選ぶ
function guessTable(data) {
  let best = null;
  let bestScore = 0;
  for (const t of data.tables) {
    const score = t.fields.filter((f) => guessHeader(f, data.headers)).length;
    if (score > bestScore) { best = t.id; bestScore = score; }
  }
  return best;
}

function guessHeader(field, headers) {
  return headers.find((h) => h === field.label) || headers.find((h) => h && (h.includes(field.label) || field.label.includes(h))) || "";
}

function drawMapping() {
  const p = screenState.preview;
  const box = document.getElementById("import-preview");
  if (!p) { box.replaceChildren(); return; }
  const d = p.data;
  const tableDef = d.tables.find((t) => t.id === document.getElementById("import-table").value) || d.tables[0];
  const selects = {};
  const rows = tableDef.fields.map((f) => {
    const chosen = tableDef.saved ? tableDef.saved[f.name] || "" : guessHeader(f, d.headers);
    const select = el("select", {});
    options(select, d.headers.filter(Boolean).map((h) => [h, h]), { blank: "（使わない）", value: chosen });
    if (!chosen) select.value = "";
    selects[f.name] = select;
    return el("tr", {}, el("td", {}, f.label, f.required ? el("span", { class: "tag warn" }, "必須") : null), el("td", {}, select));
  });
  const sample = table(d.headers.map((h) => h || "（空）"), d.sample.map((r) => el("tr", {}, r.map((c) => el("td", {}, c)))), "grid");
  box.replaceChildren(
    el("div", { class: "detail-title" }, `${d.filename}${d.sheet ? `（シート ${d.sheet}）` : ""}・${d.row_count} 行・列 ${d.headers.length}`),
    table(["モックの項目", "元の列"], rows),
    el("h3", {}, `先頭 ${d.sample.length} 行`),
    el("div", { class: "table-scroll sample" }, sample),
    el("div", { class: "form-row" }, el("button", {
      type: "button", class: "primary",
      onclick: () => {
        const missing = tableDef.fields.filter((f) => f.required && !selects[f.name].value).map((f) => f.label);
        if (missing.length) { showMessage(`必須の列が対応づいていない: ${missing.join("・")}`); return; }
        screenState.pending.push({
          filename: d.filename, content: p.content, sheet: d.sheet, table: tableDef.id, tableLabel: tableDef.label,
          columns: Object.fromEntries(Object.entries(selects).filter(([, s]) => s.value).map(([k, s]) => [k, s.value])),
        });
        screenState.preview = null;
        document.getElementById("import-file").value = "";
        document.getElementById("import-sheet-label").hidden = true;
        drawMapping();
        drawPending();
        showMessage("");
      },
    }, "この内容で一覧に加える")),
  );
}

function drawPending() {
  const box = document.getElementById("import-pending");
  if (!screenState.pending.length) { box.replaceChildren(el("p", { class: "muted" }, "なし")); return; }
  box.replaceChildren(table(["ファイル", "表", "対応づけ", ""], screenState.pending.map((f, i) => el("tr", {},
    el("td", {}, `${f.filename}${f.sheet ? `（${f.sheet}）` : ""}`),
    el("td", {}, f.tableLabel),
    el("td", { class: "muted" }, Object.entries(f.columns).map(([k, v]) => `${k}←${v}`).join("・")),
    el("td", {}, el("button", { type: "button", onclick: () => { screenState.pending.splice(i, 1); drawPending(); } }, "外す")),
  ))));
}

async function runImport() {
  if (!screenState.pending.length) throw new Error("取り込む一覧が空");
  const result = await api("POST", "/api/import", {
    version_id: document.getElementById("import-version").value,
    note: document.getElementById("import-note").value,
    files: screenState.pending.map((f) => ({ filename: f.filename, content: f.content, sheet: f.sheet, table: f.table, columns: f.columns })),
  });
  screenState.pending = [];
  drawPending();
  const c = result.counts;
  document.getElementById("import-result").replaceChildren(
    el("div", { class: "detail-title" }, `版 ${result.version_id} を取り込みました: 品目 ${c.items}・構成行 ${c.lines}・原価 ${c.costs}・年間台数 ${c.volumes}`),
    warningList(result.warnings),
  );
  await refreshLists();
  await drawVersions();
}

function warningList(warnings) {
  if (!warnings.length) return el("p", { class: "muted" }, "警告なし");
  return el("div", {}, el("p", { class: "missing" }, `検証の警告 ${warnings.length} 件`), table(["ファイル", numTh("行"), "内容"], warnings.map((w) => el("tr", {},
    el("td", {}, w.file || "—"), numTd(w.row, { missing: "—" }), el("td", {}, w.message)))));
}

async function drawVersions() {
  const box = document.getElementById("version-list");
  if (!state.versions.length) { box.replaceChildren(el("p", { class: "muted" }, "なし")); return; }
  const detail = el("div", {});
  box.replaceChildren(
    table(["版", "取り込んだ日時", "元のファイル", "メモ", numTh("警告"), ""], state.versions.map((v) => el("tr", {},
      el("td", {}, v.version_id),
      el("td", {}, v.imported_at ? v.imported_at.replace("T", " ") : "—"),
      el("td", {}, v.source_name || "—"),
      el("td", {}, v.note || ""),
      el("td", { class: `num ${v.warning_count ? "missing" : ""}` }, String(v.warning_count)),
      el("td", {},
        el("button", { type: "button", onclick: () => guarded(async () => {
          const info = await api("GET", `/api/versions/info?${qs({ version: v.version_id })}`);
          detail.replaceChildren(
            el("div", { class: "detail-title" }, `版 ${info.version_id} の検証の結果`),
            info.used_by.length ? el("p", { class: "muted" }, `使っている試算: ${info.used_by.join("・")}`) : null,
            warningList(info.warnings),
          );
        }) }, "警告を見る"),
        el("button", { type: "button", onclick: () => guarded(async () => {
          if (!confirm(`版 ${v.version_id} を消しますか？（元に戻せません）`)) return;
          await api("DELETE", `/api/versions?${qs({ version: v.version_id })}`);
          await refreshLists();
          await drawVersions();
          showMessage(`版 ${v.version_id} を消しました。`, "info");
        }) }, "消す")),
    ))),
    detail,
  );
}

// 配線 ----------------------------------------------------------------------

function wireScreens() {
  document.querySelectorAll("#screen-seg button").forEach((b) => b.addEventListener("click", () => guarded(() => showScreen(b.dataset.screen))));
  document.getElementById("part-show").addEventListener("click", () => guarded(drawPart));
  document.getElementById("part-code").addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawPart));
  document.getElementById("part-version").addEventListener("change", (e) => guarded(() => loadCodes(e.target.value)));
  document.getElementById("part-scenario").addEventListener("change", (e) => {
    const s = state.scenarios.find((x) => String(x.scenario_id) === e.target.value);
    if (s) document.getElementById("part-version").value = s.version_id;
  });
  document.getElementById("change-new-kind").addEventListener("change", changeKindToggle);
  document.getElementById("change-show").addEventListener("click", () => guarded(drawChange));
  document.getElementById("change-old-version").addEventListener("change", (e) => guarded(() => loadCodes(e.target.value)));
  document.getElementById("import-file").addEventListener("change", () => guarded(() => previewFile()));
  document.getElementById("import-sheet").addEventListener("change", (e) => guarded(() => previewFile(e.target.value)));
  document.getElementById("import-table").addEventListener("change", drawMapping);
  document.getElementById("import-run").addEventListener("click", () => guarded(runImport));
}

wireScreens();
