// 品目照会: グループと品番（名称の一部でも）1つで、その品の基本・原価・使われ方・構成・②の変更を並べる。
// app.js・screens.js の関数を使う。原価は API の Decimal の文字列をそのまま出す。
"use strict";

const LOOK_SOURCE_CLASS = { bom: "look-src-bom", master: "look-src-master" };

async function enterLookup() {
  const group = document.getElementById("look-group");
  groupOptions(group, { value: group.value || state.summary?.scenario.group_id });
  await loadCodes(group.value);
}

// 表の中の品番。押すとその品番の照会に移る
function lookLink(code) {
  if (!code) return "";
  return el("button", { type: "button", class: "look-link", title: codeTitle(lookGroup(), code),
    onclick: () => guarded(() => lookupCode(code)) }, code);
}

function lookGroup() {
  return document.getElementById("look-group").value;
}

async function lookupCode(code) {
  document.getElementById("look-q").value = code;
  await drawLookup();
}

async function drawLookup() {
  const group = lookGroup();
  if (!group) throw new Error("グループが空");
  const q = document.getElementById("look-q").value.trim();
  if (!q) throw new Error("品番が空");
  const r = await api("GET", `/api/lookup?${qs({ group, q })}`);
  const body = document.getElementById("look-body");
  if (!r.item) {
    body.replaceChildren(
      el("h3", {}, `候補（${r.candidates.length}${r.truncated ? "+" : ""} 件）`),
      table(["品番", "名称", "記号・寸法", numTh("材料費小計"), numTh("製造原価")], r.candidates.map((c) => el("tr", {}, el("td", {}, lookLink(c.code)), el("td", {}, c.name), el("td", {}, c.specification), numTd(c.material, { yen: true }), numTd(c.manufacturing, { yen: true })))),
    );
    return;
  }
  const x = r.item;
  body.replaceChildren(
    lookBasics(x), lookCosts(x), lookUsedIn(x), lookStructure(x), lookChanges(x),
  );
}

function noneRow(n) {
  return [el("tr", {}, el("td", { colspan: n, class: "muted" }, "なし"))];
}

function lookBasics(x) {
  return el(
    "section", { class: "look-section" },
    el("h3", {}, "基本"),
    el(
      "dl", { class: "kv" },
      el("dt", {}, "品番"), el("dd", {}, codeSpan(lookGroup(), x.code), x.in_bom ? null : el("span", { class: "tag warn look-only" }, "登録のみ")),
      el("dt", {}, "名称"), el("dd", {}, x.name || "—"),
      el("dt", {}, "記号・寸法"), el("dd", {}, x.specification || "—"),
      el("dt", {}, "区分"), el("dd", {}, [x.kind, x.is_product ? "製品" : null].filter(Boolean).join("・") || "—"),
      el("dt", {}, "品目"), el("dd", {}, x.item_source_label || "—"),
    ),
  );
}

// 計算値（この品を一番上にした式の値）の8費目に、取込値（登録の6費目）と出どころを並べる
function lookCosts(x) {
  const imported = new Map(x.costs.map((c) => [c.head, c]));
  return el(
    "section", { class: "look-section" },
    el("h3", {}, "原価"),
    table(
      ["費目", numTh("計算値"), numTh("取込値"), "出どころ"],
      x.calculated.map((k) => {
        const c = imported.get(k.head);
        return el(
          "tr", {},
          el("td", {}, k.label),
          el("td", { class: `num ${k.value === null ? "missing" : ""}` },
            k.incomplete && k.value !== null ? el("span", { class: "tag warn", title: "一部未登録" }, "一部") : null, fmt(k.value, { yen: true })),
          c ? numTd(c.value, { yen: true }) : el("td", { class: "num muted" }, "—"),
          el("td", {}, c ? el("span", { class: `look-src ${LOOK_SOURCE_CLASS[c.source] || "missing"}` }, c.source_label) : ""),
        );
      }),
    ),
  );
}

function lookUsedIn(x) {
  const rows = x.used_in.map((u) => el(
    "tr", {},
    el("td", {}, lookLink(u.product)),
    el("td", {}, u.product_name), el("td", {}, u.specification),
    numTd(u.quantity_per_unit),
    el("td", {}, u.parents.map((p) => el("span", { class: "look-parent" }, lookLink(p.code), ` ×${fmt(p.quantity)}`))),
  ));
  return el(
    "section", { class: "look-section" },
    el("h3", {}, `使われ方（${x.used_in.length} 製品）`),
    table(["製品", "名称", "記号・寸法", numTh("個/台"), "直の親"], rows.length ? rows : noneRow(5)),
  );
}

function lookStructure(x) {
  if (!x.structure.length) return null;
  const rows = x.structure.map((s) => el(
    "tr", {},
    el("td", { class: "look-indent", style: `padding-left:${6 + (s.level - 1) * 18}px` }, lookLink(s.code)),
    el("td", {}, s.name), el("td", {}, s.specification),
    numTd(s.quantity),
    numTd(s.usage),
  ));
  return el(
    "section", { class: "look-section" },
    el("h3", {}, "構成"),
    table(["品番", "名称", "記号・寸法", numTh("員数"), numTh(`個/${x.code}`)], rows),
  );
}

function lookChanges(x) {
  const changeRows = x.changes.map((c) => el(
    "tr", {},
    el("td", {}, lookLink(c.product)),
    el("td", {}, el("span", { class: "tag" }, c.role)),
    el("td", {}, c.text),
  ));
  return el(
    "section", { class: "look-section" },
    el("h3", {}, `②の変更（${x.changes.length} 件）`),
    table(["製品", "役割", "変更"], changeRows.length ? changeRows : noneRow(3)),
  );
}

function wireLookup() {
  document.getElementById("look-show").addEventListener("click", () => guarded(drawLookup));
  document.getElementById("look-q").addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawLookup));
  document.getElementById("look-group").addEventListener("change", (e) => guarded(async () => {
    await loadCodes(e.target.value);
    if (document.getElementById("look-q").value.trim()) await drawLookup();
  }));
}

wireLookup();
