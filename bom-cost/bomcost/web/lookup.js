// 品目照会: 版と品番（名称の一部でも）1つで、その品の基本・原価・使われ方・構成・履歴を並べる。
// app.js・screens.js の関数を使う。原価は API の Decimal の文字列をそのまま出す。
"use strict";

const LOOK_SOURCE_CLASS = { bom: "look-src-bom", dev: "look-src-dev", common: "look-src-common" };

async function enterLookup() {
  const version = document.getElementById("look-version");
  versionOptions(version, { value: version.value || state.summary?.scenario.version_id });
  await loadCodes(version.value);
}

// 表の中の品番。押すとその品番の照会に移る
function lookLink(code) {
  if (!code) return "";
  return el("button", { type: "button", class: "look-link", title: codeTitle(lookVersion(), code),
    onclick: () => guarded(() => lookupCode(code)) }, code);
}

function lookVersion() {
  return document.getElementById("look-version").value;
}

async function lookupCode(code) {
  document.getElementById("look-q").value = code;
  await drawLookup();
}

async function drawLookup() {
  const version = document.getElementById("look-version").value;
  if (!version) throw new Error("版が空");
  const q = document.getElementById("look-q").value.trim();
  if (!q) throw new Error("品番が空");
  const r = await api("GET", `/api/lookup?${qs({ version, q })}`);
  const body = document.getElementById("look-body");
  if (!r.item) {
    body.replaceChildren(
      el("h3", {}, `候補（${r.candidates.length}${r.truncated ? "+" : ""} 件）`),
      table(["品番", "名称"], r.candidates.map((c) => el("tr", {}, el("td", {}, lookLink(c.code)), el("td", {}, c.name)))),
    );
    return;
  }
  const x = r.item;
  body.replaceChildren(
    lookBasics(x), lookCosts(x), lookUsedIn(x), lookStructure(x), lookHistory(x),
  );
}

function noneRow(n) {
  return [el("tr", {}, el("td", { colspan: n, class: "muted" }, "なし"))];
}

function lookBasics(x) {
  const entries = x.entries.map((e) => `${e.dev_number} 第${e.revision}版`).join("・") || "—";
  return el(
    "section", { class: "look-section" },
    el("h3", {}, "基本"),
    el(
      "dl", { class: "kv" },
      el("dt", {}, "品番"), el("dd", {}, codeSpan(lookVersion(), x.code), x.in_bom ? null : el("span", { class: "tag warn look-only" }, "登録のみ")),
      el("dt", {}, "名称"), el("dd", {}, x.name || "—"),
      el("dt", {}, "区分"), el("dd", {}, [x.kind, x.is_product ? "製品" : null].filter(Boolean).join("・") || "—"),
      el("dt", {}, "品目"), el("dd", {}, x.item_source_label || "—"),
      el("dt", {}, "BOM 初出"), el("dd", {}, entries),
    ),
  );
}

function lookCosts(x) {
  return el(
    "section", { class: "look-section" },
    el("h3", {}, "原価"),
    table(
      ["費目", numTh("値"), "出どころ"],
      x.costs.map((c) => el(
        "tr", {},
        el("td", {}, c.label),
        numTd(c.value),
        el("td", {}, el("span", { class: `look-src ${LOOK_SOURCE_CLASS[c.source] || "missing"}` }, c.source_label)),
      )),
    ),
  );
}

function lookUsedIn(x) {
  const rows = x.used_in.map((u) => el(
    "tr", {},
    el("td", {}, lookLink(u.product)),
    el("td", {}, u.product_name),
    numTd(u.quantity_per_unit),
    el("td", {}, u.parents.map((p) => el("span", { class: "look-parent" }, lookLink(p.code), ` ×${fmt(p.quantity)}`))),
  ));
  return el(
    "section", { class: "look-section" },
    el("h3", {}, `使われ方（${x.used_in.length} 製品）`),
    table(["製品", "名称", numTh("個/台"), "直の親"], rows.length ? rows : noneRow(4)),
  );
}

function lookStructure(x) {
  if (!x.structure.length) return null;
  const rows = x.structure.map((s) => el(
    "tr", {},
    el("td", { class: "look-indent", style: `padding-left:${6 + (s.level - 1) * 18}px` }, lookLink(s.code)),
    el("td", {}, s.name),
    numTd(s.quantity),
    numTd(s.usage),
  ));
  return el(
    "section", { class: "look-section" },
    el("h3", {}, "構成"),
    table(["品番", "名称", numTh("員数"), numTh(`個/${x.code}`)], rows),
  );
}

function lookHistory(x) {
  const heads = x.costs.map((c) => [c.head, c.label]);
  const revRows = x.revisions.map((v) => el(
    "tr", { class: v.version_id === document.getElementById("look-version").value ? "look-current" : "" },
    el("td", {}, versionLabel(v.version_id), v.found ? null : el("span", { class: "muted" }, " 無し")),
    heads.map(([id]) => numTd(v.costs[id], { missing: "—" })),
  ));
  const changeRows = x.changes.map((c) => el(
    "tr", {},
    el("td", {}, c.name),
    el("td", {}, lookLink(c.product)),
    el("td", {}, versionLabel(c.version_id)),
    el("td", {}, el("span", { class: "tag" }, c.role)),
    el("td", {}, c.text),
  ));
  return el(
    "section", { class: "look-section" },
    el("h3", {}, "履歴"),
    table(["版", ...heads.map(([, label]) => numTh(label))], revRows.length ? revRows : noneRow(heads.length + 1)),
    el("h3", {}, `検討案（${x.changes.length} 件）`),
    table(["検討案", "製品", "版", "役割", "変更"], changeRows.length ? changeRows : noneRow(5)),
  );
}

function wireLookup() {
  document.getElementById("look-show").addEventListener("click", () => guarded(drawLookup));
  document.getElementById("look-q").addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawLookup));
  document.getElementById("look-version").addEventListener("change", (e) => guarded(async () => {
    await loadCodes(e.target.value);
    if (document.getElementById("look-q").value.trim()) await drawLookup();
  }));
}

wireLookup();
