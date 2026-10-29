// ③原価比較: 製品ごとに現行（版の構成）の行と、その直下に検討案ごとの変更後の行を、6費目で並べる。app.js・screens.js の関数を使う。
"use strict";

async function enterCompare() {
  const version = document.getElementById("cmp-version");
  versionOptions(version, { value: version.value || state.summary?.scenario.version_id });
  await loadCompareNames();
}

async function loadCompareNames() {
  const version = document.getElementById("cmp-version").value;
  const box = document.getElementById("cmp-names");
  const checked = new Set([...box.querySelectorAll("input:checked")].map((i) => i.value));
  const names = version ? await api("GET", `/api/group/names?${qs({ version })}`) : [];
  box.replaceChildren(...names.map((n) =>
    el("label", {}, el("input", { type: "checkbox", value: n, checked: checked.has(n) }), ` ${n}`)));
}

async function drawCompareTable() {
  const version = document.getElementById("cmp-version").value;
  if (!version) throw new Error("版が空");
  const names = [...document.querySelectorAll("#cmp-names input:checked")].map((i) => i.value);
  if (!names.length) throw new Error("検討案を選ぶ");
  const ignore = document.getElementById("cmp-ignore").checked;
  const r = await api("GET", `/api/group/compare?${qs({
    version, names: JSON.stringify(names), q: document.getElementById("cmp-q").value.trim(),
    ignore_amortization: ignore ? "1" : "",
  })}`);
  const rows = r.rows.map((x) => el(
    "tr",
    { class: x.kind === "trial" ? "compare-trial" : "" },
    el("td", {}, x.kind === "trial" ? "" : x.product),
    el("td", {}, x.kind === "trial" ? "" : x.product_name),
    el("td", {}, x.kind === "trial" ? el("span", { class: "tag add" }, `↳ 変更後（${x.name}）`) : "現行"),
    r.heads.map((h) => numTd(x.costs[h.id], h.id === "AMORTIZATION" && r.ignore_amortization ? { missing: "—" } : undefined)),
  ));
  const heads = ["品番", "名称", "", ...r.heads.map((h) => numTh(h.label))];
  document.getElementById("cmp-body").replaceChildren(
    el("h3", {}, `製品（${countText(r.count, r.rows.filter((x) => x.kind === "base").length, r.truncated)}）`),
    table(heads, rows.length ? rows : [el("tr", {}, el("td", { colspan: heads.length, class: "muted" }, "なし"))]),
  );
}

function wireCompare() {
  document.getElementById("cmp-show").addEventListener("click", () => guarded(drawCompareTable));
  document.getElementById("cmp-q").addEventListener("keydown", (e) => e.key === "Enter" && guarded(drawCompareTable));
  document.getElementById("cmp-version").addEventListener("change", () => guarded(loadCompareNames));
}

wireCompare();
